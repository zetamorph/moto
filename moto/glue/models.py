import time
from collections import OrderedDict
from datetime import datetime
from uuid import uuid4

from moto.core import BaseBackend, BaseModel
from moto.core.models import get_account_id
from moto.core.utils import BackendDict
from moto.glue.exceptions import (
    CrawlerRunningException,
    CrawlerNotRunningException,
    SchemaVersionNotFoundFromSchemaVersionIdException,
    SchemaVersionNotFoundFromSchemaIdException,
    SchemaNotFoundException,
    SchemaVersionMetadataAlreadyExistsException,
)
from .exceptions import (
    JsonRESTError,
    CrawlerAlreadyExistsException,
    CrawlerNotFoundException,
    DatabaseAlreadyExistsException,
    DatabaseNotFoundException,
    TableAlreadyExistsException,
    TableNotFoundException,
    PartitionAlreadyExistsException,
    PartitionNotFoundException,
    VersionNotFoundException,
    JobNotFoundException,
    ConcurrentRunsExceededException,
)
from .utils import PartitionFilter
from .glue_schema_registry_utils import (
    validate_registry_id,
    validate_schema_id,
    validate_schema_params,
    get_schema_version_if_definition_exists,
    validate_registry_params,
    validate_schema_version_params,
    validate_schema_definition_length,
    validate_schema_version_metadata_pattern_and_length,
    validate_number_of_schema_version_metadata_allowed,
    get_put_schema_version_metadata_response,
    validate_register_schema_version_params,
    delete_schema_response,
)
from .glue_schema_registry_constants import (
    DEFAULT_REGISTRY_NAME,
    AVAILABLE_STATUS,
    DELETING_STATUS,
)
from ..utilities.paginator import paginate
from ..utilities.tagging_service import TaggingService


class GlueBackend(BaseBackend):
    PAGINATION_MODEL = {
        "list_crawlers": {
            "input_token": "next_token",
            "limit_key": "max_results",
            "limit_default": 100,
            "unique_attribute": "name",
        },
        "list_jobs": {
            "input_token": "next_token",
            "limit_key": "max_results",
            "limit_default": 100,
            "unique_attribute": "name",
        },
    }

    def __init__(self, region_name, account_id):
        super().__init__(region_name, account_id)
        self.databases = OrderedDict()
        self.crawlers = OrderedDict()
        self.jobs = OrderedDict()
        self.job_runs = OrderedDict()
        self.tagger = TaggingService()
        self.registries = OrderedDict()
        self.num_schemas = 0
        self.num_schema_versions = 0

    @staticmethod
    def default_vpc_endpoint_service(service_region, zones):
        """Default VPC endpoint service."""
        return BaseBackend.default_vpc_endpoint_service_factory(
            service_region, zones, "glue"
        )

    def create_database(self, database_name, database_input):
        if database_name in self.databases:
            raise DatabaseAlreadyExistsException()

        database = FakeDatabase(database_name, database_input)
        self.databases[database_name] = database
        return database

    def get_database(self, database_name):
        try:
            return self.databases[database_name]
        except KeyError:
            raise DatabaseNotFoundException(database_name)

    def get_databases(self):
        return [self.databases[key] for key in self.databases] if self.databases else []

    def delete_database(self, database_name):
        if database_name not in self.databases:
            raise DatabaseNotFoundException(database_name)
        del self.databases[database_name]

    def create_table(self, database_name, table_name, table_input):
        database = self.get_database(database_name)

        if table_name in database.tables:
            raise TableAlreadyExistsException()

        table = FakeTable(database_name, table_name, table_input)
        database.tables[table_name] = table
        return table

    def get_table(self, database_name, table_name):
        database = self.get_database(database_name)
        try:
            return database.tables[table_name]
        except KeyError:
            raise TableNotFoundException(table_name)

    def get_tables(self, database_name):
        database = self.get_database(database_name)
        return [table for table_name, table in database.tables.items()]

    def delete_table(self, database_name, table_name):
        database = self.get_database(database_name)
        try:
            del database.tables[table_name]
        except KeyError:
            raise TableNotFoundException(table_name)
        return {}

    def create_crawler(
        self,
        name,
        role,
        database_name,
        description,
        targets,
        schedule,
        classifiers,
        table_prefix,
        schema_change_policy,
        recrawl_policy,
        lineage_configuration,
        configuration,
        crawler_security_configuration,
        tags,
    ):
        if name in self.crawlers:
            raise CrawlerAlreadyExistsException()

        crawler = FakeCrawler(
            name=name,
            role=role,
            database_name=database_name,
            description=description,
            targets=targets,
            schedule=schedule,
            classifiers=classifiers,
            table_prefix=table_prefix,
            schema_change_policy=schema_change_policy,
            recrawl_policy=recrawl_policy,
            lineage_configuration=lineage_configuration,
            configuration=configuration,
            crawler_security_configuration=crawler_security_configuration,
            tags=tags,
            backend=self,
        )
        self.crawlers[name] = crawler

    def get_crawler(self, name):
        try:
            return self.crawlers[name]
        except KeyError:
            raise CrawlerNotFoundException(name)

    def get_crawlers(self):
        return [self.crawlers[key] for key in self.crawlers] if self.crawlers else []

    @paginate(pagination_model=PAGINATION_MODEL)
    def list_crawlers(self):
        return [crawler for _, crawler in self.crawlers.items()]

    def start_crawler(self, name):
        crawler = self.get_crawler(name)
        crawler.start_crawler()

    def stop_crawler(self, name):
        crawler = self.get_crawler(name)
        crawler.stop_crawler()

    def delete_crawler(self, name):
        try:
            del self.crawlers[name]
        except KeyError:
            raise CrawlerNotFoundException(name)

    def create_job(
        self,
        name,
        role,
        command,
        description,
        log_uri,
        execution_property,
        default_arguments,
        non_overridable_arguments,
        connections,
        max_retries,
        allocated_capacity,
        timeout,
        max_capacity,
        security_configuration,
        tags,
        notification_property,
        glue_version,
        number_of_workers,
        worker_type,
    ):
        self.jobs[name] = FakeJob(
            name,
            role,
            command,
            description,
            log_uri,
            execution_property,
            default_arguments,
            non_overridable_arguments,
            connections,
            max_retries,
            allocated_capacity,
            timeout,
            max_capacity,
            security_configuration,
            tags,
            notification_property,
            glue_version,
            number_of_workers,
            worker_type,
            backend=self,
        )
        return name

    def get_job(self, name):
        try:
            return self.jobs[name]
        except KeyError:
            raise JobNotFoundException(name)

    def start_job_run(self, name):
        job = self.get_job(name)
        return job.start_job_run()

    def get_job_run(self, name, run_id):
        job = self.get_job(name)
        return job.get_job_run(run_id)

    @paginate(pagination_model=PAGINATION_MODEL)
    def list_jobs(self):
        return [job for _, job in self.jobs.items()]

    def get_tags(self, resource_id):
        return self.tagger.get_tag_dict_for_resource(resource_id)

    def tag_resource(self, resource_arn, tags):
        tags = TaggingService.convert_dict_to_tags_input(tags or {})
        self.tagger.tag_resource(resource_arn, tags)

    def untag_resource(self, resource_arn, tag_keys):
        self.tagger.untag_resource_using_names(resource_arn, tag_keys)

    def create_registry(self, registry_name, description=None, tags=None):
        """CreateRegistry API"""
        # If registry name id default-registry, create default-registry
        if registry_name == DEFAULT_REGISTRY_NAME:
            registry = FakeRegistry(registry_name, description, tags)
            self.registries[registry_name] = registry
            return registry

        # Validate Registry Parameters
        validate_registry_params(self.registries, registry_name, description, tags)

        registry = FakeRegistry(registry_name, description, tags)
        self.registries[registry_name] = registry
        return registry.as_dict()

    def create_schema(
        self,
        registry_id,
        schema_name,
        data_format,
        compatibility,
        schema_definition,
        description=None,
        tags=None,
    ):
        """CrateSchema API"""
        """
        The following parameters/features are not yet implemented: Glue Schema Registry: compatibility checks NONE | BACKWARD | BACKWARD_ALL | FORWARD | FORWARD_ALL | FULL | FULL_ALL and  Data format parsing and syntax validation.
        ....
        """

        # Validate Registry Id
        registry_name = validate_registry_id(registry_id, self.registries)
        if (
            registry_name == DEFAULT_REGISTRY_NAME
            and DEFAULT_REGISTRY_NAME not in self.registries
        ):
            self.create_registry(registry_name)
        registry = self.registries[registry_name]

        # Validate Schema Parameters
        validate_schema_params(
            registry,
            schema_name,
            data_format,
            compatibility,
            schema_definition,
            self.num_schemas,
            description,
            tags,
        )

        # Create Schema
        schema_version = FakeSchemaVersion(
            registry_name, schema_name, schema_definition, version_number=1
        )
        schema_version_id = schema_version.get_schema_version_id()
        schema = FakeSchema(
            registry_name,
            schema_name,
            data_format,
            compatibility,
            schema_version_id,
            description,
            tags,
        )
        registry.schemas[schema_name] = schema
        self.num_schemas += 1

        schema.schema_versions[schema.schema_version_id] = schema_version
        self.num_schema_versions += 1

        return schema.as_dict()

    def register_schema_version(self, schema_id, schema_definition):
        """RegisterSchemaVersion API"""
        # Validate Schema Id
        registry_name, schema_name, schema_arn = validate_schema_id(
            schema_id, self.registries
        )

        compatibility = (
            self.registries[registry_name].schemas[schema_name].compatibility
        )
        data_format = self.registries[registry_name].schemas[schema_name].data_format

        validate_register_schema_version_params(
            registry_name,
            schema_name,
            schema_arn,
            self.num_schema_versions,
            schema_definition,
            compatibility,
            data_format,
        )

        # If the same schema definition is already stored in Schema Registry as a version,
        # the schema ID of the existing schema is returned to the caller.
        schema_versions = (
            self.registries[registry_name].schemas[schema_name].schema_versions.values()
        )
        existing_schema_version = get_schema_version_if_definition_exists(
            schema_versions, data_format, schema_definition
        )
        if existing_schema_version:
            return existing_schema_version

        # Register Schema Version
        version_number = (
            self.registries[registry_name]
            .schemas[schema_name]
            .get_next_schema_version()
        )
        self.registries[registry_name].schemas[schema_name].update_next_schema_version()

        self.registries[registry_name].schemas[
            schema_name
        ].update_latest_schema_version()
        self.num_schema_versions += 1

        schema_version = FakeSchemaVersion(
            registry_name, schema_name, schema_definition, version_number
        )
        self.registries[registry_name].schemas[schema_name].schema_versions[
            schema_version.schema_version_id
        ] = schema_version

        return schema_version.as_dict()

    def get_schema_version(
        self, schema_id=None, schema_version_id=None, schema_version_number=None
    ):
        """GetSchemaVersion API"""

        # Validate Schema Parameters
        (
            schema_version_id,
            registry_name,
            schema_name,
            schema_arn,
            version_number,
            latest_version,
        ) = validate_schema_version_params(
            self.registries, schema_id, schema_version_id, schema_version_number
        )

        # GetSchemaVersion using SchemaVersionId
        if schema_version_id:
            for registry in self.registries.values():
                for schema in registry.schemas.values():
                    if (
                        schema.schema_versions.get(schema_version_id, None)
                        and schema.schema_versions[
                            schema_version_id
                        ].schema_version_status
                        != DELETING_STATUS
                    ):
                        get_schema_version_dict = schema.schema_versions[
                            schema_version_id
                        ].get_schema_version_as_dict()
                        get_schema_version_dict["DataFormat"] = schema.data_format
                        return get_schema_version_dict
            raise SchemaVersionNotFoundFromSchemaVersionIdException(schema_version_id)

        # GetSchemaVersion using VersionNumber
        schema = self.registries[registry_name].schemas[schema_name]
        for schema_version in schema.schema_versions.values():
            if (
                version_number == schema_version.version_number
                and schema_version.schema_version_status != DELETING_STATUS
            ):
                get_schema_version_dict = schema_version.get_schema_version_as_dict()
                get_schema_version_dict["DataFormat"] = schema.data_format
                return get_schema_version_dict
        raise SchemaVersionNotFoundFromSchemaIdException(
            registry_name, schema_name, schema_arn, version_number, latest_version
        )

    def get_schema_by_definition(self, schema_id, schema_definition):
        """GetSchemaByDefinition API"""
        # Validate SchemaId
        validate_schema_definition_length(schema_definition)
        registry_name, schema_name, schema_arn = validate_schema_id(
            schema_id, self.registries
        )

        # Get Schema By Definition
        schema = self.registries[registry_name].schemas[schema_name]
        for schema_version in schema.schema_versions.values():
            if (
                schema_definition == schema_version.schema_definition
                and schema_version.schema_version_status != DELETING_STATUS
            ):
                get_schema_by_definition_dict = (
                    schema_version.get_schema_by_definition_as_dict()
                )
                get_schema_by_definition_dict["DataFormat"] = schema.data_format
                return get_schema_by_definition_dict
        raise SchemaNotFoundException(schema_name, registry_name, schema_arn)

    def put_schema_version_metadata(
        self, schema_id, schema_version_number, schema_version_id, metadata_key_value
    ):
        """PutSchemaVersionMetadata API"""
        # Validate metadata_key_value and schema version params
        (
            metadata_key,
            metadata_value,
        ) = validate_schema_version_metadata_pattern_and_length(metadata_key_value)
        (
            schema_version_id,
            registry_name,
            schema_name,
            schema_arn,
            version_number,
            latest_version,
        ) = validate_schema_version_params(
            self.registries, schema_id, schema_version_id, schema_version_number
        )

        # PutSchemaVersionMetadata using SchemaVersionId
        if schema_version_id:
            for registry in self.registries.values():
                for schema in registry.schemas.values():
                    if schema.schema_versions.get(schema_version_id, None):
                        metadata = schema.schema_versions[schema_version_id].metadata
                        validate_number_of_schema_version_metadata_allowed(metadata)

                        if metadata_key in metadata:
                            if metadata_value in metadata[metadata_key]:
                                raise SchemaVersionMetadataAlreadyExistsException(
                                    schema_version_id, metadata_key, metadata_value
                                )
                            metadata[metadata_key].append(metadata_value)
                        else:
                            metadata[metadata_key] = [metadata_value]
                        return get_put_schema_version_metadata_response(
                            schema_id,
                            schema_version_number,
                            schema_version_id,
                            metadata_key_value,
                        )

            raise SchemaVersionNotFoundFromSchemaVersionIdException(schema_version_id)

        # PutSchemaVersionMetadata using VersionNumber
        schema = self.registries[registry_name].schemas[schema_name]
        for schema_version in schema.schema_versions.values():
            if version_number == schema_version.version_number:
                validate_number_of_schema_version_metadata_allowed(
                    schema_version.metadata
                )
                if metadata_key in schema_version.metadata:
                    if metadata_value in schema_version.metadata[metadata_key]:
                        raise SchemaVersionMetadataAlreadyExistsException(
                            schema_version.schema_version_id,
                            metadata_key,
                            metadata_value,
                        )
                    schema_version.metadata[metadata_key].append(metadata_value)
                else:
                    schema_version.metadata[metadata_key] = [metadata_value]
                return get_put_schema_version_metadata_response(
                    schema_id,
                    schema_version_number,
                    schema_version_id,
                    metadata_key_value,
                )

        raise SchemaVersionNotFoundFromSchemaIdException(
            registry_name, schema_name, schema_arn, version_number, latest_version
        )

    def delete_schema(self, schema_id):
        """DeleteSchema API"""
        # Validate schema_id
        registry_name, schema_name, _ = validate_schema_id(schema_id, self.registries)

        # delete schema pre-processing
        schema = self.registries[registry_name].schemas[schema_name]
        num_schema_version_in_schema = len(schema.schema_versions)
        schema.schema_status = DELETING_STATUS
        response = delete_schema_response(
            schema.schema_name, schema.schema_arn, schema.schema_status
        )

        # delete schema
        del self.registries[registry_name].schemas[schema_name]
        self.num_schemas -= 1
        self.num_schema_versions -= num_schema_version_in_schema

        return response


class FakeDatabase(BaseModel):
    def __init__(self, database_name, database_input):
        self.name = database_name
        self.input = database_input
        self.created_time = datetime.utcnow()
        self.tables = OrderedDict()

    def as_dict(self):
        return {
            "Name": self.name,
            "Description": self.input.get("Description"),
            "LocationUri": self.input.get("LocationUri"),
            "Parameters": self.input.get("Parameters"),
            "CreateTime": self.created_time.isoformat(),
            "CreateTableDefaultPermissions": self.input.get(
                "CreateTableDefaultPermissions"
            ),
            "TargetDatabase": self.input.get("TargetDatabase"),
            "CatalogId": self.input.get("CatalogId"),
        }


class FakeTable(BaseModel):
    def __init__(self, database_name, table_name, table_input):
        self.database_name = database_name
        self.name = table_name
        self.partitions = OrderedDict()
        self.created_time = datetime.utcnow()
        self.versions = []
        self.update(table_input)

    def update(self, table_input):
        self.versions.append(table_input)

    def get_version(self, ver):
        try:
            if not isinstance(ver, int):
                # "1" goes to [0]
                ver = int(ver) - 1
        except ValueError as e:
            raise JsonRESTError("InvalidInputException", str(e))

        try:
            return self.versions[ver]
        except IndexError:
            raise VersionNotFoundException()

    def as_dict(self, version=-1):
        obj = {
            "DatabaseName": self.database_name,
            "Name": self.name,
            "CreateTime": self.created_time.isoformat(),
        }
        obj.update(self.get_version(version))
        return obj

    def create_partition(self, partiton_input):
        partition = FakePartition(self.database_name, self.name, partiton_input)
        key = str(partition.values)
        if key in self.partitions:
            raise PartitionAlreadyExistsException()
        self.partitions[str(partition.values)] = partition

    def get_partitions(self, expression):
        """See https://docs.aws.amazon.com/glue/latest/webapi/API_GetPartitions.html
        for supported expressions.

        Expression caveats:

        - Column names must consist of UPPERCASE, lowercase, dots and underscores only.
        - Nanosecond expressions on timestamp columns are rounded to microseconds.
        - Literal dates and timestamps must be valid, i.e. no support for February 31st.
        - LIKE expressions are converted to Python regexes, escaping special characters.
          Only % and _ wildcards are supported, and SQL escaping using [] does not work.
        """
        return list(filter(PartitionFilter(expression, self), self.partitions.values()))

    def get_partition(self, values):
        try:
            return self.partitions[str(values)]
        except KeyError:
            raise PartitionNotFoundException()

    def update_partition(self, old_values, partiton_input):
        partition = FakePartition(self.database_name, self.name, partiton_input)
        key = str(partition.values)
        if old_values == partiton_input["Values"]:
            # Altering a partition in place. Don't remove it so the order of
            # returned partitions doesn't change
            if key not in self.partitions:
                raise PartitionNotFoundException()
        else:
            removed = self.partitions.pop(str(old_values), None)
            if removed is None:
                raise PartitionNotFoundException()
            if key in self.partitions:
                # Trying to update to overwrite a partition that exists
                raise PartitionAlreadyExistsException()
        self.partitions[key] = partition

    def delete_partition(self, values):
        try:
            del self.partitions[str(values)]
        except KeyError:
            raise PartitionNotFoundException()


class FakePartition(BaseModel):
    def __init__(self, database_name, table_name, partiton_input):
        self.creation_time = time.time()
        self.database_name = database_name
        self.table_name = table_name
        self.partition_input = partiton_input
        self.values = self.partition_input.get("Values", [])

    def as_dict(self):
        obj = {
            "DatabaseName": self.database_name,
            "TableName": self.table_name,
            "CreationTime": self.creation_time,
        }
        obj.update(self.partition_input)
        return obj


class FakeCrawler(BaseModel):
    def __init__(
        self,
        name,
        role,
        database_name,
        description,
        targets,
        schedule,
        classifiers,
        table_prefix,
        schema_change_policy,
        recrawl_policy,
        lineage_configuration,
        configuration,
        crawler_security_configuration,
        tags,
        backend,
    ):
        self.name = name
        self.role = role
        self.database_name = database_name
        self.description = description
        self.targets = targets
        self.schedule = schedule
        self.classifiers = classifiers
        self.table_prefix = table_prefix
        self.schema_change_policy = schema_change_policy
        self.recrawl_policy = recrawl_policy
        self.lineage_configuration = lineage_configuration
        self.configuration = configuration
        self.crawler_security_configuration = crawler_security_configuration
        self.state = "READY"
        self.creation_time = datetime.utcnow()
        self.last_updated = self.creation_time
        self.version = 1
        self.crawl_elapsed_time = 0
        self.last_crawl_info = None
        self.arn = f"arn:aws:glue:us-east-1:{get_account_id()}:crawler/{self.name}"
        self.backend = backend
        self.backend.tag_resource(self.arn, tags)

    def get_name(self):
        return self.name

    def as_dict(self):
        last_crawl = self.last_crawl_info.as_dict() if self.last_crawl_info else None
        data = {
            "Name": self.name,
            "Role": self.role,
            "Targets": self.targets,
            "DatabaseName": self.database_name,
            "Description": self.description,
            "Classifiers": self.classifiers,
            "RecrawlPolicy": self.recrawl_policy,
            "SchemaChangePolicy": self.schema_change_policy,
            "LineageConfiguration": self.lineage_configuration,
            "State": self.state,
            "TablePrefix": self.table_prefix,
            "CrawlElapsedTime": self.crawl_elapsed_time,
            "CreationTime": self.creation_time.isoformat(),
            "LastUpdated": self.last_updated.isoformat(),
            "LastCrawl": last_crawl,
            "Version": self.version,
            "Configuration": self.configuration,
            "CrawlerSecurityConfiguration": self.crawler_security_configuration,
        }

        if self.schedule:
            data["Schedule"] = {
                "ScheduleExpression": self.schedule,
                "State": "SCHEDULED",
            }

        if self.last_crawl_info:
            data["LastCrawl"] = self.last_crawl_info.as_dict()

        return data

    def start_crawler(self):
        if self.state == "RUNNING":
            raise CrawlerRunningException(
                f"Crawler with name {self.name} has already started"
            )
        self.state = "RUNNING"

    def stop_crawler(self):
        if self.state != "RUNNING":
            raise CrawlerNotRunningException(
                f"Crawler with name {self.name} isn't running"
            )
        self.state = "STOPPING"


class LastCrawlInfo(BaseModel):
    def __init__(
        self, error_message, log_group, log_stream, message_prefix, start_time, status
    ):
        self.error_message = error_message
        self.log_group = log_group
        self.log_stream = log_stream
        self.message_prefix = message_prefix
        self.start_time = start_time
        self.status = status

    def as_dict(self):
        return {
            "ErrorMessage": self.error_message,
            "LogGroup": self.log_group,
            "LogStream": self.log_stream,
            "MessagePrefix": self.message_prefix,
            "StartTime": self.start_time,
            "Status": self.status,
        }


class FakeJob:
    def __init__(
        self,
        name,
        role,
        command,
        description=None,
        log_uri=None,
        execution_property=None,
        default_arguments=None,
        non_overridable_arguments=None,
        connections=None,
        max_retries=None,
        allocated_capacity=None,
        timeout=None,
        max_capacity=None,
        security_configuration=None,
        tags=None,
        notification_property=None,
        glue_version=None,
        number_of_workers=None,
        worker_type=None,
        backend=None,
    ):
        self.name = name
        self.description = description
        self.log_uri = log_uri
        self.role = role
        self.execution_property = execution_property
        self.command = command
        self.default_arguments = default_arguments
        self.non_overridable_arguments = non_overridable_arguments
        self.connections = connections
        self.max_retries = max_retries
        self.allocated_capacity = allocated_capacity
        self.timeout = timeout
        self.state = "READY"
        self.max_capacity = max_capacity
        self.security_configuration = security_configuration
        self.notification_property = notification_property
        self.glue_version = glue_version
        self.number_of_workers = number_of_workers
        self.worker_type = worker_type
        self.created_on = datetime.utcnow()
        self.last_modified_on = datetime.utcnow()
        self.arn = f"arn:aws:glue:us-east-1:{get_account_id()}:job/{self.name}"
        self.backend = backend
        self.backend.tag_resource(self.arn, tags)

    def get_name(self):
        return self.name

    def as_dict(self):
        return {
            "Name": self.name,
            "Description": self.description,
            "LogUri": self.log_uri,
            "Role": self.role,
            "CreatedOn": self.created_on.isoformat(),
            "LastModifiedOn": self.last_modified_on.isoformat(),
            "ExecutionProperty": self.execution_property,
            "Command": self.command,
            "DefaultArguments": self.default_arguments,
            "NonOverridableArguments": self.non_overridable_arguments,
            "Connections": self.connections,
            "MaxRetries": self.max_retries,
            "AllocatedCapacity": self.allocated_capacity,
            "Timeout": self.timeout,
            "MaxCapacity": self.max_capacity,
            "WorkerType": self.worker_type,
            "NumberOfWorkers": self.number_of_workers,
            "SecurityConfiguration": self.security_configuration,
            "NotificationProperty": self.notification_property,
            "GlueVersion": self.glue_version,
        }

    def start_job_run(self):
        if self.state == "RUNNING":
            raise ConcurrentRunsExceededException(
                f"Job with name {self.name} already running"
            )
        fake_job_run = FakeJobRun(job_name=self.name)
        self.state = "RUNNING"
        return fake_job_run.job_run_id

    def get_job_run(self, run_id):
        fake_job_run = FakeJobRun(job_name=self.name, job_run_id=run_id)
        return fake_job_run


class FakeJobRun:
    def __init__(
        self,
        job_name: int,
        job_run_id: str = "01",
        arguments: dict = None,
        allocated_capacity: int = None,
        timeout: int = None,
        worker_type: str = "Standard",
    ):
        self.job_name = job_name
        self.job_run_id = job_run_id
        self.arguments = arguments
        self.allocated_capacity = allocated_capacity
        self.timeout = timeout
        self.worker_type = worker_type
        self.started_on = datetime.utcnow()
        self.modified_on = datetime.utcnow()
        self.completed_on = datetime.utcnow()

    def get_name(self):
        return self.job_name

    def as_dict(self):
        return {
            "Id": self.job_run_id,
            "Attempt": 1,
            "PreviousRunId": "01",
            "TriggerName": "test_trigger",
            "JobName": self.job_name,
            "StartedOn": self.started_on.isoformat(),
            "LastModifiedOn": self.modified_on.isoformat(),
            "CompletedOn": self.completed_on.isoformat(),
            "JobRunState": "SUCCEEDED",
            "Arguments": self.arguments or {"runSpark": "spark -f test_file.py"},
            "ErrorMessage": "",
            "PredecessorRuns": [
                {"JobName": "string", "RunId": "string"},
            ],
            "AllocatedCapacity": self.allocated_capacity or 123,
            "ExecutionTime": 123,
            "Timeout": self.timeout or 123,
            "MaxCapacity": 123.0,
            "WorkerType": self.worker_type,
            "NumberOfWorkers": 123,
            "SecurityConfiguration": "string",
            "LogGroupName": "test/log",
            "NotificationProperty": {"NotifyDelayAfter": 123},
            "GlueVersion": "0.9",
        }


class FakeRegistry(BaseModel):
    def __init__(self, registry_name, description=None, tags=None):
        self.name = registry_name
        self.description = description
        self.tags = tags
        self.created_time = datetime.utcnow()
        self.updated_time = datetime.utcnow()
        self.status = "AVAILABLE"
        self.registry_arn = (
            f"arn:aws:glue:us-east-1:{get_account_id()}:registry/{self.name}"
        )
        self.schemas = OrderedDict()

    def as_dict(self):
        return {
            "RegistryArn": self.registry_arn,
            "RegistryName": self.name,
            "Description": self.description,
            "Tags": self.tags,
        }


class FakeSchema(BaseModel):
    def __init__(
        self,
        registry_name,
        schema_name,
        data_format,
        compatibility,
        schema_version_id,
        description=None,
        tags=None,
    ):
        self.registry_name = registry_name
        self.registry_arn = (
            f"arn:aws:glue:us-east-1:{get_account_id()}:registry/{self.registry_name}"
        )
        self.schema_name = schema_name
        self.schema_arn = f"arn:aws:glue:us-east-1:{get_account_id()}:schema/{self.registry_name}/{self.schema_name}"
        self.description = description
        self.data_format = data_format
        self.compatibility = compatibility
        self.schema_checkpoint = 1
        self.latest_schema_version = 1
        self.next_schema_version = 2
        self.schema_status = AVAILABLE_STATUS
        self.tags = tags
        self.schema_version_id = schema_version_id
        self.schema_version_status = AVAILABLE_STATUS
        self.created_time = datetime.utcnow()
        self.updated_time = datetime.utcnow()
        self.schema_versions = OrderedDict()

    def update_next_schema_version(self):
        self.next_schema_version += 1

    def update_latest_schema_version(self):
        self.latest_schema_version += 1

    def get_next_schema_version(self):
        return self.next_schema_version

    def as_dict(self):
        return {
            "RegistryArn": self.registry_arn,
            "RegistryName": self.registry_name,
            "SchemaName": self.schema_name,
            "SchemaArn": self.schema_arn,
            "DataFormat": self.data_format,
            "Compatibility": self.compatibility,
            "SchemaCheckpoint": self.schema_checkpoint,
            "LatestSchemaVersion": self.latest_schema_version,
            "NextSchemaVersion": self.next_schema_version,
            "SchemaStatus": self.schema_status,
            "SchemaVersionId": self.schema_version_id,
            "SchemaVersionStatus": self.schema_version_status,
            "Description": self.description,
            "Tags": self.tags,
        }


class FakeSchemaVersion(BaseModel):
    def __init__(self, registry_name, schema_name, schema_definition, version_number):
        self.registry_name = registry_name
        self.schema_name = schema_name
        self.schema_arn = f"arn:aws:glue:us-east-1:{get_account_id()}:schema/{self.registry_name}/{self.schema_name}"
        self.schema_definition = schema_definition
        self.schema_version_status = AVAILABLE_STATUS
        self.version_number = version_number
        self.schema_version_id = str(uuid4())
        self.created_time = datetime.utcnow()
        self.updated_time = datetime.utcnow()
        self.metadata = OrderedDict()

    def get_schema_version_id(self):
        return self.schema_version_id

    def as_dict(self):
        return {
            "SchemaVersionId": self.schema_version_id,
            "VersionNumber": self.version_number,
            "Status": self.schema_version_status,
        }

    def get_schema_version_as_dict(self):
        # add data_format for full return dictionary of get_schema_version
        return {
            "SchemaVersionId": self.schema_version_id,
            "SchemaDefinition": self.schema_definition,
            "SchemaArn": self.schema_arn,
            "VersionNumber": self.version_number,
            "Status": self.schema_version_status,
            "CreatedTime": str(self.created_time),
        }

    def get_schema_by_definition_as_dict(self):
        # add data_format for full return dictionary of get_schema_by_definition
        return {
            "SchemaVersionId": self.schema_version_id,
            "SchemaArn": self.schema_arn,
            "Status": self.schema_version_status,
            "CreatedTime": str(self.created_time),
        }


glue_backends = BackendDict(
    GlueBackend, "glue", use_boto3_regions=False, additional_regions=["global"]
)
glue_backend = glue_backends["global"]
