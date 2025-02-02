#
# Copyright (c) 2022 Airbyte, Inc., all rights reserved.
#

from airbyte_cdk import AirbyteLogger
from airbyte_cdk.destinations import Destination
from airbyte_cdk.models import AirbyteConnectionStatus, AirbyteMessage, ConfiguredAirbyteCatalog, DestinationSyncMode, Status, Type
from datetime import datetime
import impala.dbapi
import impala.hiveserver2 as hs2
import json
from logging import getLogger
from typing import Any, Dict, Iterable, Mapping, Optional
from uuid import uuid4

from .writer import create_hive_writer


def establish_connection(config: json, logger: AirbyteLogger) -> hs2.HiveServer2Connection:
    """
    Creates a connection to Hive database using the parameters provided.
    :param config: Json object containing db credentials.
    :param logger: AirbyteLogger instance to print logs.
    :return: PEP-249 compliant database Connection object.
    """
    logger.info("Connecting to Hive")
    hive_conn = None
    try:
        if (not config["auth_type"]):
            hive_conn = impala.dbapi.connect(
                host=config["host"],
                port=config["port"]
            )
        elif (config["auth_type"].upper() == 'LDAP'):
            hive_conn = impala.dbapi.connect(
                host=config["host"],
                port=config["port"],
                auth_mechanism='LDAP',
                use_http_transport=config["use_http_transport"],
                user=config["user"],
                password=config["password"],
                use_ssl=config["use_ssl"],
                http_path=config["http_path"]
            )
    except Exception as e:
        logger.error("Error connecting to hive", str(e))
    return hive_conn

class DestinationHive(Destination):
    def write(
        self, config: Mapping[str, Any], configured_catalog: ConfiguredAirbyteCatalog, input_messages: Iterable[AirbyteMessage]
    ) -> Iterable[AirbyteMessage]:

        """
        Reads the input stream of messages, config, and catalog to write data to the destination.

        This method returns an iterable (typically a generator of AirbyteMessages via yield) containing state messages received
        in the input message stream. Outputting a state message means that every AirbyteRecordMessage which came before it has been
        successfully persisted to the destination. This is used to ensure fault tolerance in the case that a sync fails before fully completing,
        then the source is given the last state message output from this method as the starting point of the next sync.

        :param config: dict of JSON configuration matching the configuration declared in spec.json
        :param configured_catalog: The Configured Catalog describing the schema of the data being received and how it should be persisted in the
                                    destination
        :param input_messages: The stream of input messages received from the source
        :return: Iterable of AirbyteStateMessages wrapped in AirbyteMessage structs
        """

        streams = {s.stream.name for s in configured_catalog.streams}
        logger = getLogger("airbyte")
        with establish_connection(config, logger) as connection:
            writer = create_hive_writer(connection, config, logger)

            for configured_stream in configured_catalog.streams:
                if configured_stream.destination_sync_mode == DestinationSyncMode.overwrite:
                    writer.delete_table(configured_stream.stream.name)
                    logger.info(f"Stream {configured_stream.stream.name} is wiped.")
                writer.create_raw_table(configured_stream.stream.name)

            for message in input_messages:
                if message.type == Type.STATE:
                    yield message
                elif message.type == Type.RECORD:
                    data = message.record.data
                    stream = message.record.stream
                    # Skip unselected streams
                    if stream not in streams:
                        logger.debug(f"Stream {stream} was not present in configured streams, skipping")
                        continue
                    writer.queue_write_data(stream, str(uuid4()), datetime.now(), json.dumps(data))

            # Flush any leftover messages
            writer.flush()

    def check(self, logger: AirbyteLogger, config: Mapping[str, Any]) -> AirbyteConnectionStatus:
        """
        Tests if the input configuration can be used to successfully connect to the destination with the needed permissions
            e.g: if a provided API token or password can be used to connect and write to the destination.

        :param logger: Logging object to display debug/info/error to the logs
            (logs will not be accessible via airbyte UI if they are not passed to this logger)
        :param config: Json object containing the configuration of this destination, content of this json is as specified in
        the properties of the spec.json file

        :return: AirbyteConnectionStatus indicating a Success or Failure
        """

        try:
            with establish_connection(config, logger) as connection:
                # We can only verify correctness of connection parameters on execution
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    print(cursor.fetchall())
                    cursor.close()
                # Test access to the bucket, if S3 strategy is used
                create_hive_writer(connection, config, logger)

            return AirbyteConnectionStatus(status=Status.SUCCEEDED)
        except Exception as e:
            return AirbyteConnectionStatus(status=Status.FAILED, message=f"An exception occurred: {repr(e)}")