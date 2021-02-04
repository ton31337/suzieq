import os
from typing import List
import logging
from pathlib import Path
import pyarrow.dataset as ds
from itertools import zip_longest
from datetime import timedelta
from contextlib import suppress

import pandas as pd
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from suzieq.db.base_db import SqDB
from suzieq.utils import Schema, SchemaForTable

from .pq_coalesce import SqCoalesceState, coalesce_resource_table


class SqParquetDB(SqDB):

    def __init__(self, cfg: dict, logger: logging.Logger) -> None:
        '''Init the Parquet DB object'''
        self.cfg = cfg
        self.logger = logger or logging.getLogger()

    def supported_data_formats(self):
        return ['pandas']

    def read(self, table_name: str, data_format: str,
             **kwargs) -> pd.DataFrame:
        """Read the data specified from parquet files and return

        This function also implements predicate pushdown to filter the data
        as specified by the provided filters.

        :param table_name: str, the name of the table to be read
        :param data_format: str, Format the data's to be returned in,
                            (only pandas supported at this point)
        :param columns: List[str], list of columns requested to be read,
                        only those specified are returned, keyword arg
        :param key_fields: List[str], key fields for table, required to
                           deduplicate, keyword arg only
        :param view: str, one of ["latest", "all"], keyword arg only
        :param start: float, starting time window for data, timestamp,
                      can be 0 to indicate latest, keyword arg only
        :param end: float, ending time window for data, timestamp,
                    can be 0 to indicate latest, keyword arg only,
        :param kwargs: dict, the optional keyword arguments, addnl_filter,
                       and merge_fields, not needed typically
        :returns: pandas dataframe of the data specified, or None if
                  unsupported format
        :rtype: pd.DataFrame

        """

        if data_format not in self.supported_data_formats():
            return None

        start = kwargs.pop("start_time")
        end = kwargs.pop("end_time")
        view = kwargs.pop("view")
        fields = kwargs.pop("columns")
        key_fields = kwargs.pop("key_fields")
        addnl_filter = kwargs.pop("add_filter", None)
        merge_fields = kwargs.pop('merge_fields', {})

        folder = self._get_table_directory(table_name, False)

        if addnl_filter:
            # This is for special cases that are specific to an object
            query_str = addnl_filter
        else:
            query_str = None

        if query_str is None:
            # Make up a dummy query string to avoid if/then/else
            query_str = "timestamp != 0"

        # If sqvers is in the requested data, we've to handle it separately
        if 'sqvers' in fields:
            fields.remove('sqvers')
            need_sqvers = True
            max_vers = 0
        else:
            need_sqvers = False

        # If requesting a specific version of the data, handle that diff too
        sqvers = kwargs.pop('sqvers', None)
        datasets = []
        try:
            dirs = Path(folder)
            try:
                for elem in dirs.iterdir():
                    # Additional processing around sqvers filtering and data
                    if 'sqvers=' not in str(elem):
                        continue
                    if sqvers and f'sqvers={sqvers}' != elem:
                        continue
                    elif need_sqvers:
                        vers = float(str(elem).split('=')[-1])
                        if vers > max_vers:
                            max_vers = vers

                    datasets.append(ds.dataset(elem, format='parquet',
                                               partitioning='hive'))
            except FileNotFoundError:
                pass
            except Exception as e:
                raise e

            # Now find the exact set of files we need to go over
            cp_dataset = self._get_cp_dataset(table_name, need_sqvers, sqvers,
                                              view, start, end)
            if cp_dataset:
                datasets.append(cp_dataset)

            if not datasets:
                datasets = [ds.dataset(folder, format='parquet',
                                       partitioning='hive')]

            # Build the filters for predicate pushdown
            master_schema = self._build_master_schema(datasets)

            avail_fields = list(filter(lambda x: x in master_schema.names,
                                       fields))

            filters = self.build_ds_filters(
                start, end, master_schema, merge_fields=merge_fields, **kwargs)

            final_df = ds.dataset(datasets) \
                .to_table(filter=filters, columns=avail_fields) \
                .to_pandas(self_destruct=True) \
                .query(query_str) \
                .sort_values(by='timestamp')

            if merge_fields:
                # These are key fields that need to be set right before we do
                # the drop duplicates to avoid missing out all the data
                for field in merge_fields:
                    newfld = merge_fields[field]
                    if (field in final_df.columns and
                            newfld in final_df.columns):
                        final_df[newfld] = np.where(final_df[newfld],
                                                    final_df[newfld],
                                                    final_df[field])
                    elif (field in final_df.columns and
                          newfld not in final_df.columns):
                        final_df = final_df.rename(columns={field: newfld})

            # Because of how coalescing works, we can have multiple duplicated
            # entries with same timestamp. Remove them
            dupts_keys = key_fields + ['timestamp']
            final_df = final_df.set_index(dupts_keys) \
                               .query('~index.duplicated(keep="last")') \
                               .reset_index()
            if (not final_df.empty and (view == 'latest') and
                    all(x in final_df.columns for x in key_fields)):
                final_df = final_df.set_index(key_fields) \
                                   .query('~index.duplicated(keep="last")')
        except (pa.lib.ArrowInvalid, OSError):
            return pd.DataFrame(columns=fields)

        if need_sqvers:
            final_df['sqvers'] = max_vers
            fields.insert(0, 'sqvers')

        cols = set(final_df.columns.tolist() + final_df.index.names)
        fields = [x for x in fields if x in cols]
        return final_df.reset_index()[fields]

    def write(self, table_name: str, data_format: str,
              data, coalesced: bool, schema: pa.lib.Schema,
              filename_cb, **kwargs) -> int:
        """Write the data supplied as a dataframe as a parquet file

        :param cfg: Suzieq configuration
        :param table_name: str, Name of the table to write data to
        :param data: data to be written, usually pandas DF, but can be
                     engine specific (spark, dask etc.)
        :param data_format: str, Format the data's to be returned in,
                            (only pandas supported at this point)
        :param coalesced: bool, True if data being written is in compacted form
        :param schema: pa.Schema, the schema for the data
        :param filename_cb: callable, callback function to create the filename
        :returns: status of write
        :rtype: integer

        """
        folder = self._get_table_directory(table_name, coalesced)
        if coalesced:
            partition_cols = ['sqvers', 'namespace']
        else:
            partition_cols = ['sqvers', 'namespace', 'hostname']
        if data_format == "pandas":
            if isinstance(data, pd.DataFrame):
                table = pa.Table.from_pandas(data, schema=schema,
                                             preserve_index=False)
            elif isinstance(data, pa.Table):
                table = data
            elif isinstance(data, dict):
                df = pd.DataFrame.from_dict(data["records"])
                table = pa.Table.from_pandas(df, schema=schema,
                                             preserve_index=False)

            if filename_cb:
                pq.write_to_dataset(table, root_path=folder,
                                    partition_cols=partition_cols,
                                    version="2.0", compression="ZSTD",
                                    row_group_size=100000)
            else:
                pq.write_to_dataset(table, root_path=folder,
                                    partition_cols=partition_cols,
                                    version="2.0", compression="ZSTD",
                                    partition_filename_cb=filename_cb,
                                    row_group_size=100000)

        return 0

    def coalesce(self, tables: List[str] = [], period: str = '',
                 ign_sqpoller: bool = False) -> None:
        """Coalesce all the resource parquet files in specified folder.

        This routine does not run periodically. It runs once and returns.

        :param tables: List[str], List of specific tables to coalesce, empty for all
        :param period: str, coalescing period, needed for various internal stuff
        :param ign_sqpoller: True if its OK to ignore the absence of sqpoller to
                             coalesce
        :returns: Nothing
        """

        infolder = self.cfg['data-directory']
        outfolder = self._get_table_directory('', True)  # root folder
        archive_folder = self.cfg.get('coalescer', {}) \
                                 .get('archive-directory',
                                      f'{infolder}/_archived')
        if not period:
            period = self.cfg.get(
                'coalesceer', {'period': '1h'}).get('period', '1h')
        schemas = Schema(self.cfg.get('schema-directory'))
        state = SqCoalesceState(self.logger, period)

        state.logger = self.logger
        # Trying to be complete here. the ignore prefixes assumes you have coalesceers
        # across multiple time periods running, and so we need to ignore the files
        # created by the longer time period coalesceions. In other words, weekly
        # coalesceer should ignore monthly and yearly coalesced files, monthly
        # coalesceer should ignore yearly coalesceer and so on.
        try:
            timeint = int(period[:-1])
            time_unit = period[-1]
            if time_unit == 'h':
                run_int = timedelta(hours=timeint)
                state.prefix = 'sqc-h-'
                state.ign_pfx = ['.', '_', 'sqc-']
            elif time_unit == 'd':
                run_int = timedelta(days=timeint)
                if timeint > 364:
                    state.prefix = 'sqc-y-'
                    state.ign_pfx = ['.', '_', 'sqc-y-']
                elif timeint > 29:
                    state.prefix = 'sqc-m-'
                    state.ign_pfx = ['.', '_', 'sqc-m-', 'sqc-y-']
                else:
                    state.prefix = 'sqc-d-'
                    state.ign_pfx = ['.', '_', 'sqc-d-', 'sqc-w-', 'sqc-m-',
                                     'sqc-y-']
            elif time_unit == 'w':
                run_int = timedelta(weeks=timeint)
                state.prefix = 'sqc-w-'
                state.ign_pfx = ['.', '_', 'sqc-w-', 'sqc-m-', 'sqc-y-']
            else:
                logging.error(f'Invalid unit for period, {time_unit}, '
                              'must be one of h/d/w')
        except ValueError:
            logging.error(f'Invalid time, {period}')
            return

        state.period = run_int
        # Create list of tables to coalesce.
        # TODO: Verify that we're only coalescing parquet tables here
        if tables:
            tables = [x for x in tables
                      if schemas.tables() and
                      (schemas.type_for_table(x) != "derivedRecord")]
        else:
            tables = [x for x in schemas.tables()
                      if schemas.type_for_table(x) != "derivedRecord"]
        if 'sqPoller' not in tables and not ign_sqpoller:
            # This is an error. sqPoller is how we keep track of discontinuities
            # among other things.
            self.logger.error(
                'No sqPoller data, cannot compute discontinuities')
            return
        else:
            # We want sqPoller to be first to compute discontinuities
            with suppress(ValueError):
                tables.remove('sqPoller')
            if not ign_sqpoller:
                tables.insert(0, 'sqPoller')

        # We've forced the sqPoller to be always the first table to be coalesced
        for entry in tables:
            table_outfolder = f'{outfolder}/{entry}'
            table_infolder = f'{infolder}//{entry}'
            table_archive_folder = f'{archive_folder}/{entry}'
            if not os.path.isdir(table_infolder):
                self.logger.info(
                    f'No input records to coalesce for {entry}')
                continue
            if not os.path.isdir(table_outfolder):
                os.makedirs(table_outfolder)
            if not os.path.isdir(table_archive_folder):
                os.makedirs(table_archive_folder, exist_ok=True)
            coalesce_resource_table(table_infolder, table_outfolder,
                                    table_archive_folder, entry,
                                    SchemaForTable(entry, schemas, None),
                                    state)
            self.logger.info(
                f'coalesced {state.wrfile_count} files/{state.wrrec_count} '
                f'of {entry}')
        return

    def _get_cp_dataset(self, table_name: str, need_sqvers: bool,
                        sqvers: str, view: str, start_time: float,
                        end_time: float) -> ds.dataset:
        """Get the list of files to read in

        This iterates over the coalesced files that need to be read and comes
        up with a list of files that corresponds to the timeslot the user has
        specified

        :param table_name: str, Table for which coalesced info is requested
        :param need_sqvers: bool, True if the user has requested that we return the
                            sqvers
        :param sqvers: str, if we're looking only for files of a specific version
        :param view: str, whether to return the latest only OR all
        :param start_time: float, the starting time window for which data is sought
        : param end_time: float, the ending time window for which data is sought
        :returns: pyarrow dataset for the files to be read
        :rtype: pyarrow.dataset.dataset

        """

        filelist = []
        max_vers = 0

        folder = self._get_table_directory(table_name, True)

        if start_time and end_time or (view == "all"):
            # Enforcing the logic we have: if both start_time & end_time
            # are given, return all files since the model is that the user is
            # expecting to see all changes in the time window. Otherwise, the user
            # is expecting to see only the latest before an end_time OR after a
            # start_time.
            all_files = True
        else:
            all_files = False

        # We need to iterate otherwise the differing schema from different dirs
        # causes the read to abort.
        dirs = Path(folder)
        if not dirs.exists() or not dirs.is_dir():
            return

        for elem in dirs.iterdir():
            # Additional processing around sqvers filtering and data
            if 'sqvers=' not in str(elem):
                continue
            if sqvers and f'sqvers={sqvers}' != elem:
                continue
            elif need_sqvers:
                vers = float(str(elem).split('=')[-1])
                if vers > max_vers:
                    max_vers = vers

            dataset = ds.dataset(elem, format='parquet', partitioning='hive')
            if not start_time and not end_time:
                files = dataset.files
            else:
                files = []
                latest_filedict = {}
                prev_time = 0
                prev_namespace = ''
                file_in_this_ns = False
                prev_file = None
                for file in sorted(dataset.files):
                    namespace = os.path.dirname(file).split('namespace=')[1] \
                                                     .split('/')[0]
                    if (prev_namespace and (namespace != prev_namespace) and
                            not file_in_this_ns):
                        if ((start_time and thistime[1] >= start_time) or
                                (end_time and thistime[1] >= end_time)):
                            files.append(prev_file)
                            prev_namespace = ''
                    thistime = os.path.basename(file).split('.')[0] \
                                                     .split('-')[-2:]
                    thistime = [int(x)*1000 for x in thistime]  # time in ms
                    if not start_time or (thistime[0] >= start_time):
                        if not end_time:
                            files.append(file)
                            file_in_this_ns = True
                        elif thistime[0] < end_time:
                            files.append(file)
                            file_in_this_ns = True
                        elif prev_time < end_time < thistime[0]:
                            key = file.split('namespace=')[1].split('/')[0]
                            if key not in latest_filedict:
                                latest_filedict[key] = file
                                file_in_this_ns = True

                    prev_time = thistime[0]
                    prev_file = file
                    prev_namespace = namespace
                if not file_in_this_ns:
                    if ((start_time and thistime[1] >= start_time) or
                            (end_time and thistime[1] >= end_time)):
                        files.append(file)

                if latest_filedict:
                    filelist.extend(list(latest_filedict.values()))
            if not all_files and files:
                latest_filedict = {x.split('namespace=')[1].split('/')[0]: x
                                   for x in sorted(files)}
                filelist.extend(list(latest_filedict.values()))
            elif files:
                filelist.extend(sorted(files))

        if filelist:
            return ds.dataset(filelist, format='parquet', partitioning='hive')
        else:
            return None

    def _build_master_schema(self, datasets: list) -> pa.lib.Schema:
        """Build the master schema from the list of diff versions
        We use this to build the filters and use the right type-based check
        for a field.
        """
        msch = datasets[0].schema
        msch_set = set(msch)
        for dataset in datasets[1:]:
            sch = dataset.schema
            sch_set = set(sch)
            if msch_set.issuperset(sch):
                continue
            elif sch_set.issuperset(msch):
                msch = sch
            else:
                for fld in sch_set-msch_set:
                    index = sch.get_field_index(fld.name)
                    msch.insert(index, fld)

        return msch

    def _cons_int_filter(self, keyfld: str, filter_str: str) -> ds.Expression:
        '''Construct Integer filters with arithmetic operations'''
        if not isinstance(filter_str, str):
            return (ds.field(keyfld) == int(filter_str))

        # Check if we have logical operator (<, >, = etc.)
        if filter_str.startswith('<='):
            return (ds.field(keyfld) <= int(filter_str[2:]))
        elif filter_str.startswith('>='):
            return (ds.field(keyfld) >= int(filter_str[2:]))
        elif filter_str.startswith('<'):
            return (ds.field(keyfld) < int(filter_str[1:]))
        elif filter_str.startswith('>'):
            return (ds.field(keyfld) > int(filter_str[1:]))
        else:
            return (ds.field(keyfld) == int(filter_str))

    def build_ds_filters(self, start_tm: float, end_tm: float,
                         schema: pa.lib.Schema,
                         **kwargs) -> ds.Expression:
        """The new style of filters using dataset instead of ParquetDataset"""

        merge_fields = kwargs.pop('merge_fields', {})
        # The time filters first
        if start_tm and not end_tm:
            filters = ds.field("timestamp") >= start_tm
        elif end_tm and not start_tm:
            filters = ds.field("timestamp") <= end_tm
        elif start_tm and end_tm:
            filters = ((ds.field("timestamp") >= start_tm)
                       & (ds.field("timestamp") <= end_tm))
        else:
            filters = (ds.field("timestamp") != 0)

        sch_fields = schema.names
        for k, v in kwargs.items():
            if not v:
                continue
            if k not in sch_fields:
                self.logger.warning(f'Ignoring invalid field {k} in filter')
                continue

            ftype = schema.field(k).type
            if k in merge_fields:
                k = merge_fields[k]

            if isinstance(v, list):
                infld = []
                notinfld = []
                or_filters = None
                for e in v:
                    if isinstance(e, str) and e.startswith("!"):
                        if ftype == 'int64':
                            notinfld.append(int(e[1:]))
                        else:
                            notinfld.append(e[1:])
                    else:
                        if ftype == 'int64':
                            if or_filters:
                                or_filters = or_filters | \
                                    self._cons_int_filter(k, e)
                            else:
                                or_filters = self._cons_int_filter(k, e)
                        else:
                            infld.append(e)
                if infld and notinfld:
                    filters = filters & (ds.field(k).isin(infld) &
                                         ~ds.field(k).isin(notinfld))
                elif infld:
                    filters = filters & (ds.field(k).isin(infld))
                elif notinfld:
                    filters = filters & (~ds.field(k).isin(notinfld))

                if or_filters:
                    filters = filters & (or_filters)
            else:
                if isinstance(v, str) and v.startswith("!"):
                    if ftype == 'int64':
                        filters = filters & (ds.field(k) != int(v[1:]))
                    else:
                        filters = filters & (ds.field(k) != v[1:])
                else:
                    if ftype == 'int64':
                        filters = filters & self._cons_int_filter(k, v)
                    else:
                        filters = filters & (ds.field(k) == v)

        return filters

    def _get_table_directory(self, table_name: str, coalesced: bool) -> str:
        """Return the directory name for the table specified

        :param table_name: str, The table for which you want the folder
        :param coalesced: bool, True if you want the folder for the coalesced
                          data
        :returns: folder for the specified table name
        :rtype: str

        """
        if coalesced:
            dir = self.cfg.get('coalescer', {})\
                          .get('coalesce-directory',
                               f'{self.cfg.get("data-directory")}/coalesced')
        else:
            dir = f'{self.cfg.get("data-directory")}'

        if table_name:
            return f'{dir}/{table_name}'
        else:
            return dir

    def get_tables(self, **kwargs):
        """finds the tables that are available"""

        cfg = self.cfg
        if not getattr(self, 'cfg', None):
            self.cfg = cfg
        dfolder = self.cfg['data-directory']
        tables = set()
        if dfolder:
            dfolder = os.path.abspath(dfolder) + '/'
            p = Path(dfolder)
            namespaces = kwargs.get('namespace', [])
            if not namespaces:
                ns = set([x.parts[-1].split('=')[1]
                          for x in p.glob('**/namespace=*')])
            else:
                ns = set(namespaces)
            for dc in ns:
                dirlist = p.glob(f'**/namespace={dc}')
                tlist = [str(x).split(z)[1].split('/')[0]
                         for x, z in list(zip_longest(dirlist, [dfolder],
                                                      fillvalue=dfolder))]
                if not tables:
                    tables = set(tlist)
                else:
                    tables.update(tlist)
        return list(tables)
