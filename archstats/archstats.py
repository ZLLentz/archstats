import ast
import json
import logging
from functools import partial
from typing import Any, List, Optional

import inflection
from caproto import ChannelData
from caproto.server import AsyncLibraryLayer, PVGroup, pvproperty

from .db_backed import DatabaseBackedJSONRequestGroup, Request

logger = logging.getLogger(__name__)


def key_to_pv(key: str) -> str:
    """
    Take an archiver appliance JSON key and make a PV name out of it.

    Parameters
    ----------
    key : str
        The archiver appliance metrics key name.

    Example
    -------
    >>> key_to_pv("Avg time spent by getETLStreams() in ETL(0&raquo;1) (s/run)")
    'AvgTimeSpentByGetetlstreamsInEtl0To1SPerRun'
    """
    # Pre-filter: &raquo -> to
    key = key.replace("&raquo;", " to ")
    # Pre-filter: / -> per
    key = key.replace("/", " per ")
    # Pre-filter: ETL -> _ETL_
    key = key.replace("ETL", " ETL ")
    # Pre-filter: .*rate -> Rate
    if key.endswith('rate'):
        key = key[:-4] + 'Rate'

    # Parametrize it for consistency:
    parametrized = inflection.parameterize(key)
    return inflection.camelize(parametrized.replace("-", "_"))


def archiver_literal_eval(value: str) -> Any:
    """literaleval-like function for archiver metrics values."""
    try:
        evaluated = ast.literal_eval(value)
    except Exception:
        return value

    # Numbers such as: 160,732 become (160, 732)
    if isinstance(evaluated, tuple):
        return archiver_literal_eval(value.replace(',', ''))
    return evaluated


def _value_to_pvproperty_kwargs(key: str, value: Any) -> dict:
    """
    Take a value (an integer, float, bool, or string) and return the keyword
    arguments to make a new `pvproperty`.
    """
    value = archiver_literal_eval(value)

    kwargs = {
        'doc': key,
        'record': {
            bool: 'bi',
            float: 'ai',
            int: 'longin',
            str: 'stringin',
        }[type(value)],
    }

    if isinstance(value, str):
        kwargs['report_as_string'] = True
        kwargs['max_length'] = 2000

    return {
        'value': value,
        'kwargs': kwargs,
    }


def instance_metrics_to_pvproperties(metrics_string: str) -> List[dict]:
    """
    Make a key-pvproperty kwarg dictionary from a metrics JSON string.

    The instance metrics are of the form (note the surrounding list):
        [{"instance1_key1": "value1", "instance1_key2": "value2"},
         {"instance2_key1": "value1", "instance2_key2": "value2"},
         ]
    """
    def to_instance_pv(instance_dict, key):
        return instance_dict['instance'] + ':' + key_to_pv(key)

    return [
        dict(
            name=to_instance_pv(instance_dict, key),
            **_value_to_pvproperty_kwargs(key, value)
        )
        for instance_dict in json.loads(metrics_string)
        for key, value in instance_dict.items()
    ]


def detailed_metrics_to_pvproperties(instance: str, metrics_string: str) -> List[dict]:
    """
    Make a key-pvproperty kwarg dictionary from a metrics JSON string.

    These detailed metrics are a list of dictionaries with the keys: value,
    name, and source.
    """

    def load_and_filter(metrics_string):
        for item in json.loads(metrics_string):
            name = item['name']
            if name.startswith('Estimated bytes transferred in ETL'):
                # A special case - the units on these can change dynamically
                # underneath us.  Adjust to keep it always in MB.
                value = archiver_literal_eval(item['value'])

                unitless_name, units = item['name'].rsplit('(', 1)
                units = units.rstrip(')')
                if value:
                    if units == 'KB':
                        value /= 1024.0  # KB -> MB
                    elif units == 'MB':
                        ...
                    elif units == 'GB':
                        value *= 1024.0  # GB -> MB

                item['name'] = f'{unitless_name}(MB)'
                item['value'] = value

            yield item

    return [
        dict(
            name=key_to_pv(item['name']),
            **_value_to_pvproperty_kwargs(item['name'], item['value'])
        )
        for item in load_and_filter(metrics_string)
    ]


def storage_metrics_to_pvproperties(metrics_string: str) -> List[dict]:
    """
    Make a key-pvproperty kwarg dictionary from a JSON string.

    The storage metrics are of the form (note the surrounding list):
        [{"storage1_key1": "value1", "storage1_key2": "value2"},
         {"storage2_key1": "value1", "storage2_key2": "value2"},
         ]

    The short, medium, and long-term storage are marked by the "name" key as
    STS, MTS, and LTS.
    """
    def to_storage_pv(storage_dict, key):
        return storage_dict['name'] + ':' + inflection.camelize(key)

    return [
        dict(
            name=to_storage_pv(storage_dict, key),
            **_value_to_pvproperty_kwargs(key, value)
        )
        for storage_dict in json.loads(metrics_string)
        for key, value in storage_dict.items()
        if key != 'name'
    ]


def process_metrics_to_pvproperties(metrics_string: str) -> List[dict]:
    """
    Make a key-pvproperty kwarg dictionary from a JSON string.

    The process metrics are of the form (note the surrounding list):
        [{"data": [[ts, value], ...], "label": "value2"},
         ...
         ]

    .. note::

        This may be slightly inaccurate due to phase offsets/process metrics
        updating out-of-sync with our polling loop. At worst, we're
        consistently ~1 minute off.
    """

    def get_value(data):
        if not data:
            return 0

        try:
            # value from the last (timestamp, value) pair
            return data[-1][1]
        except Exception:
            return 0.0

    def to_process_info(label='unknown', data=None, **kwargs):
        return {
            "name": inflection.camelize(label.split(' ', 1)[0]),
            "value": get_value(data),
        }

    return [
        to_process_info(**metrics_dict)
        for metrics_dict in json.loads(metrics_string)

    ]


class Archstats(PVGroup):
    """
    EPICS Archiver Appliance statistics IOC.
    """

    updater = pvproperty(value=0, name='__UPDATER__', read_only=True)
    update_rate = 60

    def __init__(self, *args, appliance_url,
                 database_url='http://localhost:9200',
                 database_backend=None,
                 **kwargs):
        super().__init__(*args, **kwargs)

        self.appliance_url = appliance_url
        self.database_url = database_url
        self.database_backend = database_backend
        self._dynamic_groups = []
        self._document_count = {}

    async def __ainit__(self):
        """
        A special async init handler, finished prior to `caproto.server.run()`.
        """
        basic_metrics_req = Request(
            url=f'{self.appliance_url}mgmt/bpl/getApplianceMetrics',
            transformer=instance_metrics_to_pvproperties,

        )

        # await self._add_dynamic_group(
        #     'ApplianceMetricsGroup',
        #     basic_metrics_req,
        #     index='archiver_appliance_metrics',
        # )

        await basic_metrics_req.make()
        instances = [
            appliance_info['instance']
            for appliance_info in json.loads(basic_metrics_req.last_response.raw)
        ]

        for idx, instance in enumerate(instances):
            await self._add_dynamic_group(
                f'DetailedMetricsGroup{instance}',
                [
                    Request(
                        url=f'{self.appliance_url}mgmt/bpl/getApplianceMetricsForAppliance',
                        transformer=partial(detailed_metrics_to_pvproperties, instance),
                        parameters=dict(appliance=instance)
                    ),
                    Request(
                        url=f'{self.appliance_url}mgmt/bpl/getStorageMetricsForAppliance',
                        transformer=storage_metrics_to_pvproperties,
                        parameters=dict(appliance=instance)
                    ),
                    Request(
                        url=f'{self.appliance_url}mgmt/bpl/getProcessMetricsDataForAppliance',
                        transformer=process_metrics_to_pvproperties,
                        parameters=dict(appliance=instance)
                    ),
                ],
                index=f'archiver_appliance_metrics_{instance.lower()}',
                prefix=f'{instance}:',
            )

    async def _add_dynamic_group(
            self,
            class_name: str,
            request: Request,
            index: Optional[str] = None,
            prefix: str = '',
            ) -> DatabaseBackedJSONRequestGroup:
        """
        Add a dynamic PVGroup to be periodically updated.

        Parameters
        ----------
        class_name : str
            The class name for the new group.

        request : Request
            The request object used to make the query and generate the PV
            database.

        index : str, optional
            The index name to use.

        prefix : str, optional
            The prefix for the group, exclusive of ``self.prefix``.
        """

        group_cls = await DatabaseBackedJSONRequestGroup.from_request(
            class_name, request)
        group = group_cls(prefix=f'{self.prefix}{prefix}',
                          backend=self.database_backend,
                          url=self.database_url,
                          index=index, parent=self)

        self._dynamic_groups.append(group)
        self._document_count[group] = 0
        self._pvs_.update(group._pvs_)
        self.pvdb.update(group.pvdb)

        if hasattr(group, '__ainit__'):
            await group.__ainit__()

        return group_cls, group

    async def _update_group(self, group: DatabaseBackedJSONRequestGroup):
        """
        Update the dynamic group `group`.

        Parameters
        ----------
        group : DatabaseBackedJSONRequestGroup
            The group to update.
        """
        changed = False
        for request in group.requests:
            for item in await request.make():
                try:
                    attr = group.key_to_attr_map[item['name']]
                except KeyError:
                    self.log.warning('Saw new entry: %s', item)
                    continue

                prop = getattr(group, attr)
                try:
                    if prop.value != item['value']:
                        await prop.write(value=item['value'])  # , timestamp=timestamp)
                        changed = True
                except Exception:
                    self.log.exception('Failed to update %s to %s', prop, item)

        first_document = self._document_count[group] == 0
        if changed or (first_document and group.init_document is None):
            await group.db_helper.store()
            self._document_count[group] += 1

    @updater.startup
    async def updater(self, instance: ChannelData, async_lib: AsyncLibraryLayer):
        """
        Startup hook: periodically update the dynamic groups contained here.
        """
        while True:
            try:
                for group in self._dynamic_groups:
                    await self._update_group(group)
                    await async_lib.library.sleep(0.1)
            except Exception:
                self.log.exception('Update failed!')
                await async_lib.library.sleep(10.0)

            await async_lib.library.sleep(self.update_rate)
