"""
karapace - schema backup

Copyright (c) 2023 Aiven Ltd
See LICENSE for details
"""
from __future__ import annotations

from enum import Enum
from kafka import KafkaConsumer, KafkaProducer
from kafka.admin import KafkaAdminClient
from kafka.consumer.fetcher import ConsumerRecord
from kafka.errors import TopicAlreadyExistsError
from kafka.structs import PartitionMetadata, TopicPartition
from karapace import constants
from karapace.anonymize_schemas import anonymize_avro
from karapace.backup.consumer import PollTimeout
from karapace.backup.errors import BackupError, PartitionCountError, StaleConsumerError
from karapace.config import Config, read_config
from karapace.key_format import KeyFormatter
from karapace.schema_reader import new_schema_topic_from_config
from karapace.typing import JsonData, JsonObject
from karapace.utils import json_decode, json_encode, KarapaceKafkaClient
from pathlib import Path
from tempfile import mkstemp
from tenacity import retry, RetryCallState, stop_after_delay, wait_fixed
from typing import AbstractSet, Any, Callable, Collection, Dict, Generator, IO, List, TextIO, Tuple

import argparse
import base64
import contextlib
import logging
import os
import sys

LOG = logging.getLogger(__name__)

# Schema topic has single partition.
# Use of this in `producer.send` disables the partitioner to calculate which partition the data is sent.
PARTITION_ZERO = 0
BACKUP_VERSION_2_MARKER = "/V2\n"


class BackupVersion(Enum):
    V1 = 1
    V2 = 2


def __before_sleep(description: str) -> Callable[[RetryCallState], None]:
    """Returns a function to print a user-friendly message before going to sleep in retries.

    :param description: of the action, should compose well with _failed_ and _returned_ as next words.
    :returns: a function that can be used in ``tenacity.retry``'s ``before_sleep`` argument for printing a user-friendly
        message that explains which action failed, that a retry is going to happen, and how to abort if desired.
    """

    def before_sleep(it: RetryCallState) -> None:
        outcome = it.outcome
        if outcome is None:
            result = "did not complete yet"
        elif outcome.failed:
            result = f"failed ({outcome.exception()})"
        else:
            result = f"returned {outcome.result()!r}"
        print(f"{description} {result}, retrying... (Ctrl+C to abort)", file=sys.stderr)

    return before_sleep


def __check_partition_count(topic: str, supplier: Callable[[str], AbstractSet[PartitionMetadata]]) -> None:
    """Checks that the given topic has exactly one partition.

    :param topic: to check.
    :param supplier: of topic partition metadata.
    :raises PartitionCountError: if the topic does not have exactly one partition.
    """
    partition_count = len(supplier(topic))
    if partition_count != 1:
        raise PartitionCountError(
            f"Topic {topic!r} has {partition_count} partitions, but only topics with exactly 1 partition can be backed "
            "up. The schemas topic MUST have exactly 1 partition to ensure perfect ordering of schema updates."
        )


@contextlib.contextmanager
def _admin(config: Config) -> KafkaAdminClient:
    """Creates an automatically closing Kafka admin client.

    :param config: for the client.
    :raises Exception: if client creation fails, concrete exception types are unknown, see Kafka implementation.
    """

    @retry(
        before_sleep=__before_sleep("Kafka Admin client creation"),
        reraise=True,
        stop=stop_after_delay(60),  # seconds
        wait=wait_fixed(1),  # seconds
    )
    def __admin() -> KafkaAdminClient:
        return KafkaAdminClient(
            api_version_auto_timeout_ms=constants.API_VERSION_AUTO_TIMEOUT_MS,
            bootstrap_servers=config["bootstrap_uri"],
            client_id=config["client_id"],
            security_protocol=config["security_protocol"],
            ssl_cafile=config["ssl_cafile"],
            ssl_certfile=config["ssl_certfile"],
            ssl_keyfile=config["ssl_keyfile"],
            kafka_client=KarapaceKafkaClient,
        )

    admin = __admin()
    try:
        yield admin
    finally:
        admin.close()


@retry(
    before_sleep=__before_sleep("Schemas topic creation"),
    reraise=True,
    stop=stop_after_delay(60),  # seconds
    wait=wait_fixed(1),  # seconds
)
def _maybe_create_topic(config: Config, name: str | None = None) -> bool | None:
    """Creates the topic if the given name and the one in the config are the same.

    :param config: for the admin client.
    :param name: of the topic to create.
    :returns: ``True`` if the topic was created, ``False`` if it already exists, and ``None`` if the given name does not
        match the name of the schema topic in the config, in which case nothing has been done.
    :raises Exception: if topic creation fails, concrete exception types are unknown, see Kafka implementation.
    """
    topic = new_schema_topic_from_config(config)

    if name is not None and topic.name != name:
        LOG.warning(
            "Not creating topic, because the name %r from the config and the name %r from the CLI differ.",
            topic.name,
            name,
        )
        return None

    with _admin(config) as admin:
        try:
            admin.create_topics([topic], timeout_ms=constants.TOPIC_CREATION_TIMEOUT_MS)
            LOG.info(
                "Created topic %r (partition count: %s, replication factor: %s, config: %s)",
                topic.name,
                topic.num_partitions,
                topic.replication_factor,
                topic.topic_configs,
            )
            return True
        except TopicAlreadyExistsError:
            LOG.debug("Topic %r already exists", topic.name)
            return False


@contextlib.contextmanager
def _consumer(config: Config, topic: str) -> KafkaConsumer:
    """Creates an automatically closing Kafka consumer client.

    :param config: for the client.
    :param topic: to consume from.
    :raises PartitionCountError: if the topic does not have exactly one partition.
    :raises Exception: if client creation fails, concrete exception types are unknown, see Kafka implementation.
    """
    consumer = KafkaConsumer(
        topic,
        enable_auto_commit=False,
        bootstrap_servers=config["bootstrap_uri"],
        client_id=config["client_id"],
        security_protocol=config["security_protocol"],
        ssl_cafile=config["ssl_cafile"],
        ssl_certfile=config["ssl_certfile"],
        ssl_keyfile=config["ssl_keyfile"],
        sasl_mechanism=config["sasl_mechanism"],
        sasl_plain_username=config["sasl_plain_username"],
        sasl_plain_password=config["sasl_plain_password"],
        auto_offset_reset="earliest",
        metadata_max_age_ms=config["metadata_max_age_ms"],
        kafka_client=KarapaceKafkaClient,
    )
    try:
        __check_partition_count(topic, consumer.partitions_for_topic)
        yield consumer
    finally:
        consumer.close()


@contextlib.contextmanager
def _producer(config: Config, topic: str) -> KafkaProducer:
    """Creates an automatically closing Kafka producer client.

    :param config: for the client.
    :param topic: to produce to.
    :raises PartitionCountError: if the topic does not have exactly one partition.
    :raises Exception: if client creation fails, concrete exception types are unknown, see Kafka implementation.
    """
    producer = KafkaProducer(
        bootstrap_servers=config["bootstrap_uri"],
        security_protocol=config["security_protocol"],
        ssl_cafile=config["ssl_cafile"],
        ssl_certfile=config["ssl_certfile"],
        ssl_keyfile=config["ssl_keyfile"],
        sasl_mechanism=config["sasl_mechanism"],
        sasl_plain_username=config["sasl_plain_username"],
        sasl_plain_password=config["sasl_plain_password"],
        kafka_client=KarapaceKafkaClient,
    )
    try:
        __check_partition_count(topic, producer.partitions_for)
        yield producer
    finally:
        producer.close()


@contextlib.contextmanager
def _writer(
    file: str | Path,
    *,
    overwrite: bool | None = None,
) -> Generator[TextIO, None, None]:
    """Opens the given file for writing.

    This function uses a safe temporary file to collect all written data, followed by a final rename. On most systems
    the final rename is atomic under most conditions, but there are no guarantees. The temporary file is always created
    next to the given file, to ensure that the temporary file is on the same physical volume as the target file, and
    avoid issues that might arise when moving data between physical volumes.

    :param file: to open for writing, both the empty string and the conventional single dash ``-`` will yield
        ``sys.stdout`` instead of actually creating a file for writing.
    :param overwrite: may be set to ``True`` to overwrite an existing file at the same location.
    :raises FileExistsError: if ``overwrite`` is not ``True`` and the file already exists, or if the parent directory of
        the file is not a directory.
    :raises OSError: if writing fails or if the file already exists and is not actually a file.
    """
    if file in ("", "-"):
        yield sys.stdout
    else:
        if not isinstance(file, Path):
            file = Path(file)
        dst = file.absolute()

        def check_dst() -> None:
            if dst.exists():
                if overwrite is not True:
                    raise FileExistsError(f"--location already exists at {dst}, use --overwrite to replace the file.")
                if not dst.is_file():
                    raise FileExistsError(
                        f"--location already exists at {dst}, but is not a file and thus cannot be overwritten."
                    )

        check_dst()
        dst.parent.mkdir(parents=True, exist_ok=True)
        fd, path = mkstemp(dir=dst.parent, prefix=dst.name)
        src = Path(path)
        try:
            fp = open(fd, "w", encoding="utf8")
            try:
                yield fp
                fp.flush()
                os.fsync(fd)
            finally:
                fp.close()
            check_dst()
            # This might still fail despite all checks, because there is a time window in which other processes can make
            # changes to the filesystem while our program is advancing. However, we have done the best we can.
            src.replace(dst)
        finally:
            try:
                src.unlink()
            except FileNotFoundError:
                pass


def _check_backup_file_version(fp: IO) -> BackupVersion:
    version_identifier = fp.read(4)
    if version_identifier == BACKUP_VERSION_2_MARKER:
        # Seek back to start, readline() to consume linefeed
        fp.seek(0)
        fp.readline()
        return BackupVersion.V2
    fp.seek(0)
    return BackupVersion.V1


class SchemaBackup:
    def __init__(self, config: Config, backup_path: str, topic_option: str | None = None) -> None:
        self.config = config
        self.backup_location = backup_path
        self.topic_name: str = topic_option or self.config["topic_name"]
        self.timeout_ms = 1000
        self.timeout_kafka_producer = 5

        self.producer_exception: Exception | None = None

        # Schema key formatter
        self.key_formatter = None
        if self.topic_name == constants.DEFAULT_SCHEMA_TOPIC or self.config.get("force_key_correction", False):
            self.key_formatter = KeyFormatter()

    def restore_backup(self) -> None:
        if not os.path.exists(self.backup_location):
            raise BackupError("Backup location doesn't exist")

        _maybe_create_topic(self.config, self.topic_name)

        with _producer(self.config, self.topic_name) as producer:
            LOG.info("Starting backup restore for topic: %r", self.topic_name)

            with open(self.backup_location, encoding="utf8") as fp:
                if _check_backup_file_version(fp) == BackupVersion.V2:
                    self._restore_backup_version_2(producer, fp)
                else:
                    self._restore_backup_version_1_single_array(producer, fp)
            producer.flush(timeout=self.timeout_kafka_producer)
            if self.producer_exception is not None:
                raise BackupError("Error while producing restored messages") from self.producer_exception

    def producer_error_callback(self, exception: Exception) -> None:
        self.producer_exception = exception

    def _handle_restore_message(self, producer: KafkaProducer, item: tuple[str, str]) -> None:
        key = self.encode_key(item[0])
        value = encode_value(item[1])
        LOG.debug("Sending kafka msg key: %r, value: %r", key, value)
        producer.send(
            self.topic_name,
            key=key,
            value=value,
            partition=PARTITION_ZERO,
        ).add_errback(self.producer_error_callback)

    def _restore_backup_version_1_single_array(self, producer: KafkaProducer, fp: IO) -> None:
        raw_msg = fp.read()
        # json_decode cannot really produce tuples. Typing was added in hindsight here,
        # and it looks like _handle_restore_message has been lying about the type of
        # item for some time already.
        values = json_decode(raw_msg, List[Tuple[str, str]])

        if not values:
            return

        for item in values:
            self._handle_restore_message(producer, item)

    def _restore_backup_version_2(self, producer: KafkaProducer, fp: IO) -> None:
        for line in fp:
            hex_key, hex_value = (val.strip() for val in line.split("\t"))  # strip to remove the linefeed

            key = base64.b16decode(hex_key).decode("utf8") if hex_key != "null" else hex_key
            value = base64.b16decode(hex_value.strip()).decode("utf8") if hex_value != "null" else hex_value
            self._handle_restore_message(producer, (key, value))

    def create(
        self,
        serialize: Callable[[bytes | None, bytes | None], str],
        *,
        poll_timeout: PollTimeout | None = None,
        overwrite: bool | None = None,
    ) -> None:
        """Creates a backup of the configured topic.

        FIXME the serialize callback is obviously dangerous as part of the public API, since it cannot be guaranteed
            that it produces a string that is actually version 2 compatible. We anyway have to introduce a version 3,
            and this public API can be fixed along with the introduction of it.

        :param serialize: callback that encodes the consumer record into the target backup format.
        :param poll_timeout: specifies the maximum time to wait for receiving records, if not records are received
            within that time and the target offset has not been reached an exception is raised. Defaults to one minute.
        :param overwrite: the output file if it exists.
        :raises Exception: if consumption fails, concrete exception types are unknown, see Kafka implementation.
        :raises FileExistsError: if ``overwrite`` is not ``True`` and the file already exists, or if the parent
            directory of the file is not a directory.
        :raises OSError: if writing fails or if the file already exists and is not actually a file.
        :raises StaleConsumerError: if no records are received within the given ``poll_timeout`` and the target offset
            has not been reached yet.
        """
        if poll_timeout is None:
            poll_timeout = PollTimeout.default()
        poll_timeout_ms = poll_timeout.to_milliseconds()
        topic = self.topic_name
        with _writer(self.backup_location, overwrite=overwrite) as fp, _consumer(self.config, topic) as consumer:
            (partition,) = consumer.partitions_for_topic(self.topic_name)
            topic_partition = TopicPartition(self.topic_name, partition)
            start_offset: int = consumer.beginning_offsets([topic_partition])[topic_partition]
            end_offset: int = consumer.end_offsets([topic_partition])[topic_partition]
            last_offset = start_offset
            record_count = 0

            fp.write(BACKUP_VERSION_2_MARKER)
            if start_offset < end_offset:  # non-empty topic
                end_offset -= 1  # high watermark to actual end offset
                print(
                    "Started backup of %s:%s (offset %s to %s)...",
                    topic,
                    partition,
                    f"{start_offset:,}",
                    f"{end_offset:,}",
                    file=sys.stderr,
                )
                while True:
                    records: Collection[ConsumerRecord] = consumer.poll(poll_timeout_ms).get(topic_partition, [])
                    if len(records) == 0:
                        raise StaleConsumerError(topic_partition, start_offset, end_offset, last_offset, poll_timeout)
                    record: ConsumerRecord
                    for record in records:
                        fp.write(serialize(record.key, record.value))
                        record_count += 1
                    last_offset = record.offset
                    if last_offset >= end_offset:
                        break
            print(
                "Finished backup of %s:%s to %r (backed up %s records).",
                topic,
                partition,
                "stdout" if fp is sys.stdout else self.backup_location,
                f"{record_count:,}",
                file=sys.stderr,
            )

    def encode_key(self, key: JsonObject | str) -> bytes | None:
        if key == "null":
            return None
        if not self.key_formatter:
            if isinstance(key, str):
                return key.encode("utf8")
            return json_encode(key, sort_keys=False, binary=True, compact=False)
        if isinstance(key, str):
            key = json_decode(key, JsonObject)
        return self.key_formatter.format_key(key)


def encode_value(value: JsonData | str) -> bytes | None:
    if value == "null":
        return None
    if isinstance(value, str):
        return value.encode("utf8")
    return json_encode(value, compact=True, sort_keys=False, binary=True)


def serialize_record(key_bytes: bytes | None, value_bytes: bytes | None) -> str:
    key = base64.b16encode(key_bytes).decode("utf8") if key_bytes is not None else "null"
    value = base64.b16encode(value_bytes).decode("utf8") if value_bytes is not None else "null"
    return f"{key}\t{value}\n"


def anonymize_avro_schema_message(key_bytes: bytes | None, value_bytes: bytes | None) -> str:
    if key_bytes is None:
        raise RuntimeError("Cannot Avro-encode message with key_bytes=None")
    if value_bytes is None:
        raise RuntimeError("Cannot Avro-encode message with value_bytes=None")
    # Check that the message has key `schema` and type is Avro schema.
    # The Avro schemas may have `schemaType` key, if not present the schema is Avro.

    key = json_decode(key_bytes, Dict[str, str])
    value = json_decode(value_bytes, Dict[str, str])

    if value and "schema" in value and value.get("schemaType", "AVRO") == "AVRO":
        original_schema: Any = json_decode(value["schema"])
        anonymized_schema = anonymize_avro.anonymize(original_schema)
        if anonymized_schema:
            value["schema"] = json_encode(anonymized_schema, compact=True, sort_keys=False)
    if value and "subject" in value:
        value["subject"] = anonymize_avro.anonymize_name(value["subject"])
    # The schemas topic contain all changes to schema metadata.
    if key.get("subject", None):
        key["subject"] = anonymize_avro.anonymize_name(key["subject"])
    return serialize_record(
        json_encode(key, compact=True, binary=True),
        json_encode(value, compact=True, binary=True),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Karapace schema backup tool")
    subparsers = parser.add_subparsers(help="Schema backup command", dest="command", required=True)

    parser_get = subparsers.add_parser("get", help="Store the schema backup into a file")
    parser_restore = subparsers.add_parser("restore", help="Restore the schema backup from a file")
    parser_export_anonymized_avro_schemas = subparsers.add_parser(
        "export-anonymized-avro-schemas", help="Export anonymized Avro schemas into a file"
    )
    for p in (parser_get, parser_restore, parser_export_anonymized_avro_schemas):
        p.add_argument("--config", help="Configuration file path", required=True)
        p.add_argument("--location", default="", help="File path for the backup file")
        p.add_argument("--topic", help="Kafka topic name to be used", required=False)

    for p in (parser_get, parser_export_anonymized_avro_schemas):
        p.add_argument("--overwrite", action="store_true", help="Overwrite --location even if it exists.")
        p.add_argument("--poll-timeout", help=PollTimeout.__doc__, type=PollTimeout)

    return parser.parse_args()


def main() -> None:
    try:
        args = parse_args()

        with open(args.config, encoding="utf8") as handler:
            config = read_config(handler)

        sb = SchemaBackup(config, args.location, args.topic)

        try:
            if args.command == "get":
                sb.create(serialize_record, poll_timeout=args.poll_timeout, overwrite=args.overwrite)
            elif args.command == "restore":
                sb.restore_backup()
            elif args.command == "export-anonymized-avro-schemas":
                sb.create(anonymize_avro_schema_message, poll_timeout=args.poll_timeout, overwrite=args.overwrite)
            else:
                # Only reachable if a new subcommand was added that is not mapped above. There are other ways with
                # argparse to handle this, but all rely on the programmer doing exactly the right thing. Only switching
                # to another CLI framework would provide the ability to not handle this situation manually while
                # ensuring that it is not possible to add a new subcommand without also providing a handler for it.
                raise SystemExit(f"Entered unreachable code, unknown command: {args.command!r}")
        except StaleConsumerError as e:
            print(
                f"The Kafka consumer did not receive any records for partition {e.partition} of topic {e.topic!r} "
                f"within the poll timeout ({e.poll_timeout} seconds) while trying to reach offset {e.end_offset:,} "
                f"(start was {e.start_offset:,} and the last seen offset was {e.last_offset:,}).\n"
                "\n"
                "Try increasing --poll-timeout to give the broker more time.",
                file=sys.stderr,
            )
            raise SystemExit(1) from e
    except KeyboardInterrupt as e:
        # Not an error -- user choice -- and thus should not end up in a Python stacktrace.
        raise SystemExit(2) from e


if __name__ == "__main__":
    main()
