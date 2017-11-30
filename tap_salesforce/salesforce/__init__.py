import re
import threading
import time
import requests
from requests.exceptions import RequestException
import singer
import singer.metrics as metrics
from singer import metadata


from tap_salesforce.salesforce.bulk import Bulk
from tap_salesforce.salesforce.rest import Rest
from tap_salesforce.salesforce.exceptions import (
    TapSalesforceException,
    TapSalesforceQuotaExceededException,
    TapSalesforceHTTPException)

LOGGER = singer.get_logger()

# The minimum expiration setting for SF Refresh Tokens is 15 minutes
REFRESH_TOKEN_EXPIRATION_PERIOD = 900

BULK_API_TYPE = "BULK"
REST_API_TYPE = "REST"

STRING_TYPES = set([
    'id',
    'string',
    'picklist',
    'textarea',
    'phone',
    'url',
    'reference',
    'multipicklist',
    'combobox',
    'encryptedstring',
    'email',
    'complexvalue',  # TODO: Unverified
    'masterrecord',
    'datacategorygroupreference'
])

NUMBER_TYPES = set([
    'double',
    'currency',
    'percent'
])

DATE_TYPES = set([
    'datetime',
    'date'
])

BINARY_TYPES = set([
    'base64',
    'byte'
])

LOOSE_TYPES = set([
    'anyType',

    # A calculated field's type can be any of the supported
    # formula data types (see https://developer.salesforce.com/docs/#i1435527)
    'calculated'
])


# The following objects are not supported by the bulk API.
UNSUPPORTED_BULK_API_SALESFORCE_OBJECTS = set(['AssetTokenEvent',
                                               'AttachedContentNote',
                                               'EventWhoRelation',
                                               'QuoteTemplateRichTextData',
                                               'TaskWhoRelation',
                                               'SolutionStatus',
                                               'ContractStatus',
                                               'ContentFolderItem',
                                               'RecentlyViewed',
                                               'DeclinedEventRelation',
                                               'AcceptedEventRelation',
                                               'TaskStatus',
                                               'PartnerRole',
                                               'TaskPriority',
                                               'CaseStatus',
                                               'UndecidedEventRelation'])

# The following objects have certain WHERE clause restrictions so we exclude them.
QUERY_RESTRICTED_SALESFORCE_OBJECTS = set(['ContentDocumentLink',
                                           'CollaborationGroupRecord',
                                           'Vote',
                                           'IdeaComment',
                                           'FieldDefinition',
                                           'PlatformAction',
                                           'UserEntityAccess',
                                           'RelationshipInfo',
                                           'ContentFolderMember',
                                           'SearchLayout',
                                           'EntityParticle',
                                           'OwnerChangeOptionInfo',
                                           'DataStatistics',
                                           'UserFieldAccess',
                                           'PicklistValueInfo',
                                           'RelationshipDomain',
                                           'FlexQueueItem'])

# The following objects are not supported by the query method being used.
QUERY_INCOMPATIBLE_SALESFORCE_OBJECTS = set(['ListViewChartInstance',
                                             'FeedLike',
                                             'OutgoingEmail',
                                             'OutgoingEmailRelation',
                                             'FeedSignal',
                                             'ActivityHistory',
                                             'EmailStatus',
                                             'UserRecordAccess',
                                             'Name',
                                             'AggregateResult',
                                             'OpenActivity',
                                             'ProcessInstanceHistory',
                                             'OwnedContentDocument',
                                             'FolderedContentDocument',
                                             'FeedTrackedChange',
                                             'CombinedAttachment',
                                             'AttachedContentDocument',
                                             'ContentBody',
                                             'NoteAndAttachment',
                                             'LookedUpFromActivity'])


def field_to_property_schema(field, mdata):
    property_schema = {}

    field_name = field['name']
    sf_type = field['type']

    if sf_type in STRING_TYPES:
        property_schema['type'] = "string"
    elif sf_type in DATE_TYPES:
        property_schema["format"] = "date-time"
        property_schema['type'] = "string"
    elif sf_type == "boolean":
        property_schema['type'] = "boolean"
    elif sf_type in NUMBER_TYPES:
        property_schema['type'] = "number"
    elif sf_type == "address":
        property_schema['type'] = "object"
        property_schema['properties'] = {
            "street": {"type": ["null", "string"]},
            "state": {"type": ["null", "string"]},
            "postalCode": {"type": ["null", "string"]},
            "city": {"type": ["null", "string"]},
            "country": {"type": ["null", "string"]},
            "longitude": {"type": ["null", "number"], "multipleOf": 0.000001},
            "latitude": {"type": ["null", "number"], "multipleOf": 0.000001},
            "geocodeAccuracy": {"type": ["null", "string"]}
        }
    elif sf_type == "int":
        property_schema['type'] = "integer"
    elif sf_type == "time":
        property_schema['type'] = "string"
    elif sf_type in LOOSE_TYPES:
        return property_schema, mdata  # No type = all types
    elif sf_type in BINARY_TYPES:
        mdata = metadata.write(mdata, ('properties', field_name), "inclusion", "unsupported")
        mdata = metadata.write(mdata, ('properties', field_name),
                               "unsupported-description", "binary data")
        return property_schema, mdata
    elif sf_type == 'location':  # geo coordinates are divided into two fields for lat/long
        property_schema['type'] = "number"
        property_schema['multipleOf'] = 0.000001
    else:
        raise TapSalesforceException("Found unsupported type: {}".format(sf_type))

    # The nillable field cannot be trusted
    if field_name != 'Id':
        property_schema['type'] = ["null", property_schema['type']]

    return property_schema, mdata

class Salesforce(object):
    # pylint: disable=too-many-instance-attributes,too-many-arguments
    def __init__(self,
                 refresh_token=None,
                 token=None,
                 sf_client_id=None,
                 sf_client_secret=None,
                 quota_percent_per_run=None,
                 quota_percent_total=None,
                 is_sandbox=None,
                 select_fields_by_default=None,
                 default_start_date=None,
                 api_type=None):
        self.api_type = api_type.upper() if api_type else None
        self.refresh_token = refresh_token
        self.token = token
        self.sf_client_id = sf_client_id
        self.sf_client_secret = sf_client_secret
        self.session = requests.Session()
        self.access_token = None
        self.instance_url = None
        self.quota_percent_per_run = float(
            quota_percent_per_run) if quota_percent_per_run is not None else 25
        self.quota_percent_total = float(
            quota_percent_total) if quota_percent_total is not None else 80
        self.is_sandbox = is_sandbox == 'true'
        self.select_fields_by_default = select_fields_by_default == 'true'
        self.default_start_date = default_start_date
        self.rest_requests_attempted = 0
        self.jobs_completed = 0
        self.login_timer = None
        self.data_url = "{}/services/data/v41.0/{}"

    def _get_standard_headers(self):
        return {"Authorization": "Bearer {}".format(self.access_token)}

    # pylint: disable=anomalous-backslash-in-string,line-too-long
    def check_rest_quota_usage(self, headers):
        match = re.search('^api-usage=(\d+)/(\d+)$', headers.get('Sforce-Limit-Info'))

        if match is None:
            return

        remaining, allotted = map(int, match.groups())

        LOGGER.info("Used %s of %s daily REST API quota", remaining, allotted)

        percent_used_from_total = (remaining / allotted) * 100
        max_requests_for_run = int((self.quota_percent_per_run * allotted) / 100)

        if percent_used_from_total > self.quota_percent_total:
            total_message = ("Salesforce has reported {}/{} ({:3.2f}%) total REST quota " +
                             "used across all Salesforce Applications. Terminating " +
                             "replication to not continue past configured percentage " +
                             "of {}% total quota.").format(remaining,
                                                           allotted,
                                                           percent_used_from_total,
                                                           self.quota_percent_total)
            raise TapSalesforceQuotaExceededException(total_message)
        elif self.rest_requests_attempted > max_requests_for_run:
            partial_message = ("This replication job has made {} REST requests ({:3.2f}% of " +
                               "total quota). Terminating replication due to allotted " +
                               "quota of {}% per replication.").format(self.rest_requests_attempted,
                                                                       (self.rest_requests_attempted / allotted) * 100,
                                                                       self.quota_percent_per_run)
            raise TapSalesforceQuotaExceededException(partial_message)

    # pylint: disable=too-many-arguments
    def _make_request(self, http_method, url, headers=None, body=None, stream=False, params=None):
        if http_method == "GET":
            LOGGER.info("Making %s request to %s with params: %s", http_method, url, params)
            resp = self.session.get(url, headers=headers, stream=stream, params=params)
        elif http_method == "POST":
            LOGGER.info("Making %s request to %s with body %s", http_method, url, body)
            resp = self.session.post(url, headers=headers, data=body)
        else:
            raise TapSalesforceException("Unsupported HTTP method")

        try:
            resp.raise_for_status()
        except RequestException as ex:
            raise TapSalesforceHTTPException(ex) from ex

        if resp.headers.get('Sforce-Limit-Info') is not None:
            self.rest_requests_attempted += 1
            self.check_rest_quota_usage(resp.headers)

        return resp

    def login(self):
        if self.is_sandbox:
            login_url = 'https://test.salesforce.com/services/oauth2/token'
        else:
            login_url = 'https://login.salesforce.com/services/oauth2/token'

        login_body = {'grant_type': 'refresh_token', 'client_id': self.sf_client_id,
                      'client_secret': self.sf_client_secret, 'refresh_token': self.refresh_token}

        LOGGER.info("Attempting login via OAuth2")

        resp = self.session.post(
            login_url, data=login_body, headers={
                "Content-Type": "application/x-www-form-urlencoded"})

        try:
            resp.raise_for_status()
        except Exception as e:
            raise Exception(str(e) + ", Response from Salesforce: {}".format(resp.text)) from e

        LOGGER.info("OAuth2 login successful")

        auth = resp.json()

        self.access_token = auth['access_token']
        self.instance_url = auth['instance_url']
        self.login_timer = threading.Timer(REFRESH_TOKEN_EXPIRATION_PERIOD, self.login)
        self.login_timer.start()

    def describe(self, sobject=None):
        """Describes all objects or a specific object"""
        headers = self._get_standard_headers()
        if sobject is None:
            endpoint = "sobjects"
            endpoint_tag = "sobjects"
            url = self.data_url.format(self.instance_url, endpoint)
        else:
            endpoint = "sobjects/{}/describe".format(sobject)
            endpoint_tag = sobject
            url = self.data_url.format(self.instance_url, endpoint)

        with metrics.http_request_timer("describe") as timer:
            timer.tags['endpoint'] = endpoint_tag
            resp = self._make_request('GET', url, headers=headers)

        return resp.json()

    # pylint: disable=no-self-use
    def _get_selected_properties(self, catalog_entry):
        mdata = metadata.to_map(catalog_entry['metadata'])
        properties = catalog_entry['schema'].get('properties', {})

        return [k for k, v in properties.items()
                if metadata.get(mdata, ('properties', k), 'selected')
                or metadata.get(mdata, ('properties', k), 'inclusion') == 'automatic']

    def _get_start_date(self, state, catalog_entry):
        replication_key = catalog_entry['replication_key']

        return (singer.get_bookmark(state,
                                    catalog_entry['tap_stream_id'],
                                    replication_key) or self.default_start_date)

    def _build_query_string(self, catalog_entry, start_date, end_date=None):
        selected_properties = self._get_selected_properties(catalog_entry)

        query = "SELECT {} FROM {}".format(",".join(selected_properties), catalog_entry['stream'])

        replication_key = catalog_entry['replication_key']

        if replication_key:
            where_clause = " WHERE {} >= {} ".format(
                replication_key,
                start_date)
            if end_date:
                end_date_clause = " AND {} < {}".format(replication_key, end_date)
            else:
                end_date_clause = ""

            order_by = " ORDER BY {} ASC".format(replication_key)
            return query + where_clause + end_date_clause + order_by
        else:
            return query

    def query(self, catalog_entry, state):
        if self.api_type == BULK_API_TYPE:
            bulk = Bulk(self)
            return bulk.query(catalog_entry, state)
        elif self.api_type == REST_API_TYPE:
            rest = Rest(self)
            return rest.query(catalog_entry, state)
        else:
            raise TapSalesforceException(
                "api_type should be REST or BULK was: {}".format(
                    self.api_type))

    def get_blacklisted_objects(self):
        if self.api_type == BULK_API_TYPE:
            return UNSUPPORTED_BULK_API_SALESFORCE_OBJECTS.union(
                QUERY_RESTRICTED_SALESFORCE_OBJECTS).union(QUERY_INCOMPATIBLE_SALESFORCE_OBJECTS)
        elif self.api_type == REST_API_TYPE:
            return QUERY_RESTRICTED_SALESFORCE_OBJECTS.union(QUERY_INCOMPATIBLE_SALESFORCE_OBJECTS)
        else:
            raise TapSalesforceException(
                "api_type should be REST or BULK was: {}".format(
                    self.api_type))

    # pylint: disable=line-too-long
    def get_blacklisted_fields(self):
        if self.api_type == BULK_API_TYPE:
            return {('EntityDefinition', 'RecordTypesSupported'): "this field is unsupported by the Bulk API."}
        elif self.api_type == REST_API_TYPE:
            return {}
        else:
            raise TapSalesforceException(
                "api_type should be REST or BULK was: {}".format(
                    self.api_type))