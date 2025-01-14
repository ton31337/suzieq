import pandas as pd
import numpy as np

from suzieq.sqobjects.basicobj import SqObject
from suzieq.utils import SchemaForTable, humanize_timestamp


class OspfObj(SqObject):
    def __init__(self, **kwargs):
        super().__init__(table='ospf', **kwargs)
        self._addnl_fields = ['passive', 'area', 'state']
        self._addnl_nbr_fields = ['state']
        self._valid_get_args = ['namespace', 'hostname', 'columns',
                                'vrf', 'ifname', 'state', 'query_str']
        self._valid_assert_args = ['namespace', 'vrf', 'status']
        self._valid_arg_vals = {
            'state': ['full', 'other', 'passive', ''],
            'status': ['all', 'pass', 'fail'],
        }

    def aver(self, **kwargs):
        """Assert that the OSPF state is OK"""

        if not self.ctxt.engine:
            raise AttributeError('No analysis engine specified')

        try:
            self.validate_assert_input(**kwargs)
        except Exception as error:
            df = pd.DataFrame({'error': [f'{error}']})
            return df

        return self.engine.aver(**kwargs)

    def humanize_fields(self, df: pd.DataFrame, subset=None) -> pd.DataFrame:
        '''Humanize the timestamp and boot time fields'''
        if df.empty:
            return df

        if 'lastChangeTime' in df.columns:
            df['lastChangeTime'] = humanize_timestamp(
                df.lastChangeTime.fillna(0),
                self.cfg.get('analyzer', {}).get('timezone', None))

            if 'adjState' in df.columns:
                df['lastChangeTime'] = np.where(df.adjState == "passive",
                                                pd.Timestamp(0),
                                                df.lastChangeTime)

        return df
