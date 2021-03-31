import requests
import singer
from slugify import slugify
from singer import metadata
import urllib.parse
from copy import deepcopy
from singer.catalog import Catalog, CatalogEntry, Schema
import json

class Airtable(object):
    metadata_url = "https://api.airtable.com/v2/meta/"
    records_url = "https://api.airtable.com/v0/"
    token = None
    selected_by_default = False
    remove_emojis = False
    logger = singer.get_logger()

    @classmethod
    def run_discovery(cls, args):
        cls.__apply_config(args.config)
        if "base_id" in args.config:
            base_id = args.config['base_id']
            entries = cls.discover_base(base_id)
            return Catalog(entries).dump()

        bases = cls.__get_base_ids()
        entries = []

        for base in bases:
            entries.extend(cls.discover_base(base["id"], base["name"]))
        return Catalog(entries).dump()


    @classmethod
    def __apply_config(cls, config):
        if "metadata_url" in config:
            cls.metadata_url = config["metadata_url"]
        if "records_url" in config:
            cls.records_url = config["records_url"]
        if "selected_by_default" in config:
            cls.selected_by_default = config["selected_by_default"]
        if "remove_emojis" in config:
            cls.remove_emojis = config["remove_emojis"]
        cls.token = config["token"]

    @classmethod
    def __get_base_ids(cls):
        response = requests.get(cls.metadata_url, headers=cls.__get_auth_header())
        response.raise_for_status()
        bases = []
        for base in response.json()["bases"]:
            bases.append({
                "id": base["id"],
                "name": base["name"]
            })
        return bases


    @classmethod
    def __get_auth_header(cls):
        return {'Authorization': 'Bearer {}'.format(cls.token)}

    @classmethod
    def discover_base(cls, base_id, base_name=None):
        cls.logger.info("discover base " + base_id)
        headers = cls.__get_auth_header()
        response = requests.get(url=cls.metadata_url + base_id, headers=headers)
        response.raise_for_status()
        entries = []

        for table in response.json()["tables"]:
            schema_cols = {"id": Schema(inclusion="automatic", type=['null', "string"])}

            meta = {}

            table_name = table["name"]
            keys = []
            meta = metadata.write(meta, (), "inclusion", "available")
            meta = metadata.write(meta, 'database_name', 'base_id', base_id)

            for field in table["fields"]:
                col_schema = cls.column_schema(field)
                if col_schema.inclusion == "automatic":
                    keys.append(field["name"])
                schema_cols[field["name"]] = col_schema
                meta = metadata.write(meta, ('properties', field["name"]), 'inclusion', 'available')
                meta = metadata.write(meta, ('properties', field["name"]), 'airtable_type', field["config"]["type"] or None)

            schema = Schema(type='object', properties=schema_cols)

            entry = CatalogEntry(
                tap_stream_id=table["id"],
                database=base_name or base_id,
                table=table_name,
                stream=table_name,
                metadata=metadata.to_list(meta),
                key_properties=keys,
                schema=schema
            )
            entries.append(entry)


        return entries

    @classmethod
    def column_schema(cls, col_info):
        date_types = ["dateTime"]
        number_types = ["number", "autoNumber"]
        pk_types = ["autoNumber"]

        air_type = "string"

        if "config" in col_info and "type" in col_info["config"]:
            air_type = col_info["config"]["type"]

        inclusion = "available"
        if air_type in pk_types:
            inclusion = "automatic"

        schema = Schema(inclusion=inclusion)

        singer_type = 'string'
        if air_type in number_types:
            singer_type = 'number'

        schema.type = ['null', singer_type]

        if air_type in date_types:
            schema.format = 'date-time'
        if air_type in ["date"]:
            schema.format = 'date'

        return schema

    @classmethod
    def _find_base_id(cls, schema):
        for m in schema["metadata"]:
            if "breadcrumb" in m and m["breadcrumb"] == "database_name":
                return m["metadata"]["base_id"]

        raise Exception("catalog schema is missing base id")

    @classmethod
    def _find_selected_columns(cls, schema):
        selected_cols = {}

        for m in schema["metadata"]:
            if "properties" not in m["breadcrumb"]:
                continue

            if "selected" in m["metadata"] and m["metadata"]["selected"]:
                column_name = m["breadcrumb"][1]
                selected_cols[column_name] = schema["schema"]["properties"][column_name]
        return selected_cols

    @classmethod
    def run_sync(cls, config, properties):
        cls.__apply_config(config)

        streams = properties['streams']

        for stream in streams:
            schema = stream["schema"]["properties"]
            base_id = cls._find_base_id(stream)
            table = stream['table_name']

            table_slug = slugify(table, separator="_")

            col_defs = cls._find_selected_columns(stream)

            counter = 0
            if len(col_defs) > 0:
                cls.logger.info("will import " + table)

                response = Airtable.get_response(base_id, table, col_defs.keys(), counter=counter)
                records = response.json().get('records')

                if records:
                    col_schema = deepcopy(col_defs)
                    col_schema["id"] = schema["id"]
                    singer.write_schema(table_slug, {"properties": col_schema}, stream["key_properties"])
                    singer.write_records(table_slug, cls._map_records(schema, records))
                    offset = response.json().get("offset")

                    while offset:
                        counter += 1
                        response = Airtable.get_response(base_id, table, col_defs.keys(), offset, counter=counter)
                        records = response.json().get('records')
                        if records:
                            singer.write_records(table_slug, cls._map_records(schema, records))
                            offset = response.json().get("offset")


    @classmethod
    def _map_records(cls, schema, records):
        mapped = []

        for r in records:
            row = {}
            for col in schema:
                col_def = schema[col]
                requested_type = col_def["type"][1] or "string"
                val = r["fields"].get(col)
                if val is not None:
                    val = cls.cast_type(val, requested_type)
                row[col] = val

            row["id"] = r["id"]
            # TODO: cast to string/numbers?
            mapped.append(row)
        return mapped

    @classmethod
    def cast_type(cls, val, requested_type):
        col_type = type(val)

        if requested_type == "string" and col_type is not str:
            if col_type is float or col_type is int:
                return str(val)
            return json.dumps(val)
        return val

    @classmethod
    def get_response(cls, base_id, table, fields, offset=None, counter = 0):
        table = urllib.parse.quote(table)
        uri = cls.records_url + base_id + '/' + table

        uri += '?'
        params = {}

        if fields:
            params["fields[]"] = [f for f in list(fields) if f != "id"]
        if offset:
            params["offset"] = offset

        uri += urllib.parse.urlencode(params, True)

        response = requests.get(uri, headers=cls.__get_auth_header())

        cls.logger.info("METRIC " + json.dumps({
            "type": "counter",
            "metric": "page",
            "value": counter,
        }))
        response.raise_for_status()
        return response
