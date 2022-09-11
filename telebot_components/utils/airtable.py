import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from functools import partial
from typing import Any, Generic, Type, TypedDict, TypeVar, Union

from pyairtable import Table

logger = logging.getLogger(__name__)


AirtableColumnEnumT = TypeVar("AirtableColumnEnumT", bound=Enum)
AirtableValueT = Union[str, int, float, bool, list[str], dict]


class AirtableConfig(TypedDict):
    """Serialized Airtable table configuration.

    column_id_by_name has the following format:
    {
        "name": "fldlKmD92YqW6Pdlw",
        "age": "fldwwBJb1FSA03Yvg",
        ...
    }
    here "fldXXXXXXXXXXXXXX" values are provided by Airtable API docs, and "name", "age" etc must correspond
    to the names of AirtableColumnEnumT implementation members
    """

    base_id: str
    table_name: str
    column_id_by_name: dict[str, str]


@dataclass
class AirtableRecord(Generic[AirtableColumnEnumT]):
    """Utility class wrapping data returned from Airtable queries"""

    record_id: str
    created_at: datetime
    fields: dict[AirtableColumnEnumT, AirtableValueT]


class AirtableApi(Generic[AirtableColumnEnumT]):
    """
    pyairtable wrapper:
    - allows base/table configuration in a serializable format
    - wraps blocking operations as asynchronous with thread pool executor
    - parametrized with column name enumeration for readable queries
    """

    def __init__(
        self,
        api_key: str,
        config: AirtableConfig,
        ColumnsEnumClass: Type[AirtableColumnEnumT],
    ):
        try:
            column_id_by_enum_item = {
                ColumnsEnumClass[column_name]: column_id
                for column_name, column_id in config["column_id_by_name"].items()
            }
        except KeyError as e:
            raise KeyError(f"Failed to convert column name from config to {ColumnsEnumClass}: {e!r}\n{config = }")
        for col in ColumnsEnumClass:
            if col not in column_id_by_enum_item:
                raise ValueError(f"column_id_by_name mapping misses id for column {col.name!r}")
        self.field_id_by_column_enum_item = column_id_by_enum_item
        self.column_enum_item_by_field_id = {v: k for k, v in self.field_id_by_column_enum_item.items()}
        self.config = config
        self.ColumnsEnumClass = ColumnsEnumClass
        self.thread_pool = ThreadPoolExecutor(max_workers=16)
        self._table = Table(api_key, config["base_id"], config["table_name"])

    def _dump_entry(self, data: dict[AirtableColumnEnumT, Any]) -> dict[str, Any]:
        return {self.field_id_by_column_enum_item[k]: v for k, v in data.items()}

    async def create(self, entry: dict[AirtableColumnEnumT, Any]) -> str:
        """Returning record id as a string"""
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(self.thread_pool, self._table.create, self._dump_entry(entry))
        return result["id"]

    async def update(self, record_id: str, update: dict[AirtableColumnEnumT, Any]) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self.thread_pool, self._table.update, record_id, self._dump_entry(update))

    async def read_all(self) -> list[AirtableRecord[AirtableColumnEnumT]]:
        loop = asyncio.get_running_loop()
        raw_records: list[dict] = await loop.run_in_executor(
            self.thread_pool,
            partial(self._table.all, return_fields_by_field_id=True),
        )
        records: list[AirtableRecord] = []
        unknown_field_ids: set[str] = set()
        for raw_record in raw_records:
            try:
                record_id: str = raw_record["id"]
                created_at_str: str = raw_record["createdTime"]
                created_at = datetime.strptime(created_at_str.split(".")[0], r"%Y-%m-%dT%H:%M:%S")
                fields_raw: dict[str, AirtableValueT] = raw_record["fields"]
                fields: dict[AirtableColumnEnumT, AirtableValueT] = dict()
                for field_id, value in fields_raw.items():
                    column_enum_item = self.column_enum_item_by_field_id.get(field_id)
                    if column_enum_item is None:
                        unknown_field_ids.add(field_id)
                        continue
                    else:
                        fields[column_enum_item] = value
                records.append(AirtableRecord(record_id=record_id, created_at=created_at, fields=fields))
            except Exception:
                logger.exception(f"Error parsing retrieved airtable record! Ignoring it: {raw_record!r}")

        if unknown_field_ids:
            logger.warning(
                "Some fields retrieved from airtable have unknown ids, "
                + f"missing from {self.ColumnsEnumClass.__name__} enum, they are ignored: "
                + ", ".join(sorted(unknown_field_ids))
            )

        return records

    def get_record_url(self, record_id: str) -> str:
        return f"https://airtable.com/{self.config['base_id']}/{self.config['table_name']}/{record_id}"
