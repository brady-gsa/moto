from boto3 import Session

from moto.core import BaseBackend
from moto.core.utils import unix_time_millis
from moto.logs.metric_filters import MetricFilters
from .exceptions import (
    ResourceNotFoundException,
    ResourceAlreadyExistsException,
    InvalidParameterException,
)


class LogEvent:
    _event_id = 0

    def __init__(self, ingestion_time, log_event):
        self.ingestionTime = ingestion_time
        self.timestamp = log_event["timestamp"]
        self.message = log_event["message"]
        self.eventId = self.__class__._event_id
        self.__class__._event_id += 1
        ""

    def to_filter_dict(self):
        return {
            "eventId": str(self.eventId),
            "ingestionTime": self.ingestionTime,
            # "logStreamName":
            "message": self.message,
            "timestamp": self.timestamp,
        }

    def to_response_dict(self):
        return {
            "ingestionTime": self.ingestionTime,
            "message": self.message,
            "timestamp": self.timestamp,
        }


class LogStream:
    _log_ids = 0

    def __init__(self, region, log_group, name):
        self.region = region
        self.arn = "arn:aws:logs:{region}:{id}:log-group:{log_group}:log-stream:{log_stream}".format(
            region=region,
            id=self.__class__._log_ids,
            log_group=log_group,
            log_stream=name,
        )
        self.creationTime = int(unix_time_millis())
        self.firstEventTimestamp = None
        self.lastEventTimestamp = None
        self.lastIngestionTime = None
        self.logStreamName = name
        self.storedBytes = 0
        self.uploadSequenceToken = (
            0  # I'm  guessing this is token needed for sequenceToken by put_events
        )
        self.events = []

        self.__class__._log_ids += 1

    def _update(self):
        # events can be empty when stream is described soon after creation
        self.firstEventTimestamp = (
            min([x.timestamp for x in self.events]) if self.events else None
        )
        self.lastEventTimestamp = (
            max([x.timestamp for x in self.events]) if self.events else None
        )

    def to_describe_dict(self):
        # Compute start and end times
        self._update()

        res = {
            "arn": self.arn,
            "creationTime": self.creationTime,
            "logStreamName": self.logStreamName,
            "storedBytes": self.storedBytes,
        }
        if self.events:
            rest = {
                "firstEventTimestamp": self.firstEventTimestamp,
                "lastEventTimestamp": self.lastEventTimestamp,
                "lastIngestionTime": self.lastIngestionTime,
                "uploadSequenceToken": str(self.uploadSequenceToken),
            }
            res.update(rest)
        return res

    def put_log_events(
        self, log_group_name, log_stream_name, log_events, sequence_token
    ):
        # TODO: ensure sequence_token
        # TODO: to be thread safe this would need a lock
        self.lastIngestionTime = int(unix_time_millis())
        # TODO: make this match AWS if possible
        self.storedBytes += sum([len(log_event["message"]) for log_event in log_events])
        self.events += [
            LogEvent(self.lastIngestionTime, log_event) for log_event in log_events
        ]
        self.uploadSequenceToken += 1

        return "{:056d}".format(self.uploadSequenceToken)

    def get_log_events(
        self,
        log_group_name,
        log_stream_name,
        start_time,
        end_time,
        limit,
        next_token,
        start_from_head,
    ):
        def filter_func(event):
            if start_time and event.timestamp < start_time:
                return False

            if end_time and event.timestamp > end_time:
                return False

            return True

        def get_index_and_direction_from_token(token):
            if token is not None:
                try:
                    return token[0], int(token[2:])
                except Exception:
                    raise InvalidParameterException(
                        "The specified nextToken is invalid."
                    )
            return None, 0

        events = sorted(
            filter(filter_func, self.events), key=lambda event: event.timestamp,
        )

        direction, index = get_index_and_direction_from_token(next_token)
        limit_index = limit - 1
        final_index = len(events) - 1

        if direction is None:
            if start_from_head:
                start_index = 0
                end_index = start_index + limit_index
            else:
                end_index = final_index
                start_index = end_index - limit_index
        elif direction == "f":
            start_index = index + 1
            end_index = start_index + limit_index
        elif direction == "b":
            end_index = index - 1
            start_index = end_index - limit_index
        else:
            raise InvalidParameterException("The specified nextToken is invalid.")

        if start_index < 0:
            start_index = 0
        elif start_index > final_index:
            return (
                [],
                "b/{:056d}".format(final_index),
                "f/{:056d}".format(final_index),
            )

        if end_index > final_index:
            end_index = final_index
        elif end_index < 0:
            return (
                [],
                "b/{:056d}".format(0),
                "f/{:056d}".format(0),
            )

        events_page = [
            event.to_response_dict() for event in events[start_index : end_index + 1]
        ]

        return (
            events_page,
            "b/{:056d}".format(start_index),
            "f/{:056d}".format(end_index),
        )

    def filter_log_events(
        self,
        log_group_name,
        log_stream_names,
        start_time,
        end_time,
        limit,
        next_token,
        filter_pattern,
        interleaved,
    ):
        if filter_pattern:
            raise NotImplementedError("filter_pattern is not yet implemented")

        def filter_func(event):
            if start_time and event.timestamp < start_time:
                return False

            if end_time and event.timestamp > end_time:
                return False

            return True

        events = []
        for event in sorted(
            filter(filter_func, self.events), key=lambda x: x.timestamp
        ):
            event_obj = event.to_filter_dict()
            event_obj["logStreamName"] = self.logStreamName
            events.append(event_obj)
        return events


class LogGroup:
    def __init__(self, region, name, tags):
        self.name = name
        self.region = region
        self.arn = "arn:aws:logs:{region}:1:log-group:{log_group}".format(
            region=region, log_group=name
        )
        self.creationTime = int(unix_time_millis())
        self.tags = tags
        self.streams = dict()  # {name: LogStream}
        self.retentionInDays = (
            None  # AWS defaults to Never Expire for log group retention
        )

    def create_log_stream(self, log_stream_name):
        if log_stream_name in self.streams:
            raise ResourceAlreadyExistsException()
        self.streams[log_stream_name] = LogStream(
            self.region, self.name, log_stream_name
        )

    def delete_log_stream(self, log_stream_name):
        if log_stream_name not in self.streams:
            raise ResourceNotFoundException()
        del self.streams[log_stream_name]

    def describe_log_streams(
        self,
        descending,
        limit,
        log_group_name,
        log_stream_name_prefix,
        next_token,
        order_by,
    ):
        # responses only logStreamName, creationTime, arn, storedBytes when no events are stored.

        log_streams = [
            (name, stream.to_describe_dict())
            for name, stream in self.streams.items()
            if name.startswith(log_stream_name_prefix)
        ]

        def sorter(item):
            return (
                item[0]
                if order_by == "logStreamName"
                else item[1].get("lastEventTimestamp", 0)
            )

        if next_token is None:
            next_token = 0

        log_streams = sorted(log_streams, key=sorter, reverse=descending)
        new_token = next_token + limit
        log_streams_page = [x[1] for x in log_streams[next_token:new_token]]
        if new_token >= len(log_streams):
            new_token = None

        return log_streams_page, new_token

    def put_log_events(
        self, log_group_name, log_stream_name, log_events, sequence_token
    ):
        if log_stream_name not in self.streams:
            raise ResourceNotFoundException()
        stream = self.streams[log_stream_name]
        return stream.put_log_events(
            log_group_name, log_stream_name, log_events, sequence_token
        )

    def get_log_events(
        self,
        log_group_name,
        log_stream_name,
        start_time,
        end_time,
        limit,
        next_token,
        start_from_head,
    ):
        if log_stream_name not in self.streams:
            raise ResourceNotFoundException()
        stream = self.streams[log_stream_name]
        return stream.get_log_events(
            log_group_name,
            log_stream_name,
            start_time,
            end_time,
            limit,
            next_token,
            start_from_head,
        )

    def filter_log_events(
        self,
        log_group_name,
        log_stream_names,
        start_time,
        end_time,
        limit,
        next_token,
        filter_pattern,
        interleaved,
    ):
        streams = [
            stream
            for name, stream in self.streams.items()
            if not log_stream_names or name in log_stream_names
        ]

        events = []
        for stream in streams:
            events += stream.filter_log_events(
                log_group_name,
                log_stream_names,
                start_time,
                end_time,
                limit,
                next_token,
                filter_pattern,
                interleaved,
            )

        if interleaved:
            events = sorted(events, key=lambda event: event["timestamp"])

        if next_token is None:
            next_token = 0

        events_page = events[next_token : next_token + limit]
        next_token += limit
        if next_token >= len(events):
            next_token = None

        searched_streams = [
            {"logStreamName": stream.logStreamName, "searchedCompletely": True}
            for stream in streams
        ]
        return events_page, next_token, searched_streams

    def to_describe_dict(self):
        log_group = {
            "arn": self.arn,
            "creationTime": self.creationTime,
            "logGroupName": self.name,
            "metricFilterCount": 0,
            "storedBytes": sum(s.storedBytes for s in self.streams.values()),
        }
        # AWS only returns retentionInDays if a value is set for the log group (ie. not Never Expire)
        if self.retentionInDays:
            log_group["retentionInDays"] = self.retentionInDays
        return log_group

    def set_retention_policy(self, retention_in_days):
        self.retentionInDays = retention_in_days

    def list_tags(self):
        return self.tags if self.tags else {}

    def tag(self, tags):
        if self.tags:
            self.tags.update(tags)
        else:
            self.tags = tags

    def untag(self, tags_to_remove):
        if self.tags:
            self.tags = {
                k: v for (k, v) in self.tags.items() if k not in tags_to_remove
            }


class LogsBackend(BaseBackend):
    def __init__(self, region_name):
        self.region_name = region_name
        self.groups = dict()  # { logGroupName: LogGroup}
        self.filters = MetricFilters()

    def put_metric_filter(
        self, filter_name, filter_pattern, log_group_name, metric_transformations
    ):
        self.filters.add_filter(
            filter_name, filter_pattern, log_group_name, metric_transformations
        )

    def describe_metric_filters(self, prefix=None, log_group_name=None):
        filters = self.filters.get_matching_filters(prefix, log_group_name)
        return filters

    def delete_metric_filter(self, filter_name=None, log_group_name=None):
        self.filters.delete_filter(filter_name, log_group_name)

    def reset(self):
        region_name = self.region_name
        self.__dict__ = {}
        self.__init__(region_name)

    def create_log_group(self, log_group_name, tags):
        if log_group_name in self.groups:
            raise ResourceAlreadyExistsException()
        self.groups[log_group_name] = LogGroup(self.region_name, log_group_name, tags)

    def ensure_log_group(self, log_group_name, tags):
        if log_group_name in self.groups:
            return
        self.groups[log_group_name] = LogGroup(self.region_name, log_group_name, tags)

    def delete_log_group(self, log_group_name):
        if log_group_name not in self.groups:
            raise ResourceNotFoundException()
        del self.groups[log_group_name]

    def describe_log_groups(self, limit, log_group_name_prefix, next_token):
        if log_group_name_prefix is None:
            log_group_name_prefix = ""
        if next_token is None:
            next_token = 0

        groups = [
            group.to_describe_dict()
            for name, group in self.groups.items()
            if name.startswith(log_group_name_prefix)
        ]
        groups = sorted(groups, key=lambda x: x["creationTime"], reverse=True)
        groups_page = groups[next_token : next_token + limit]

        next_token += limit
        if next_token >= len(groups):
            next_token = None

        return groups_page, next_token

    def create_log_stream(self, log_group_name, log_stream_name):
        if log_group_name not in self.groups:
            raise ResourceNotFoundException()
        log_group = self.groups[log_group_name]
        return log_group.create_log_stream(log_stream_name)

    def delete_log_stream(self, log_group_name, log_stream_name):
        if log_group_name not in self.groups:
            raise ResourceNotFoundException()
        log_group = self.groups[log_group_name]
        return log_group.delete_log_stream(log_stream_name)

    def describe_log_streams(
        self,
        descending,
        limit,
        log_group_name,
        log_stream_name_prefix,
        next_token,
        order_by,
    ):
        if log_group_name not in self.groups:
            raise ResourceNotFoundException()
        log_group = self.groups[log_group_name]
        return log_group.describe_log_streams(
            descending,
            limit,
            log_group_name,
            log_stream_name_prefix,
            next_token,
            order_by,
        )

    def put_log_events(
        self, log_group_name, log_stream_name, log_events, sequence_token
    ):
        # TODO: add support for sequence_tokens
        if log_group_name not in self.groups:
            raise ResourceNotFoundException()
        log_group = self.groups[log_group_name]
        return log_group.put_log_events(
            log_group_name, log_stream_name, log_events, sequence_token
        )

    def get_log_events(
        self,
        log_group_name,
        log_stream_name,
        start_time,
        end_time,
        limit,
        next_token,
        start_from_head,
    ):
        if log_group_name not in self.groups:
            raise ResourceNotFoundException()
        log_group = self.groups[log_group_name]
        return log_group.get_log_events(
            log_group_name,
            log_stream_name,
            start_time,
            end_time,
            limit,
            next_token,
            start_from_head,
        )

    def filter_log_events(
        self,
        log_group_name,
        log_stream_names,
        start_time,
        end_time,
        limit,
        next_token,
        filter_pattern,
        interleaved,
    ):
        if log_group_name not in self.groups:
            raise ResourceNotFoundException()
        log_group = self.groups[log_group_name]
        return log_group.filter_log_events(
            log_group_name,
            log_stream_names,
            start_time,
            end_time,
            limit,
            next_token,
            filter_pattern,
            interleaved,
        )

    def put_retention_policy(self, log_group_name, retention_in_days):
        if log_group_name not in self.groups:
            raise ResourceNotFoundException()
        log_group = self.groups[log_group_name]
        return log_group.set_retention_policy(retention_in_days)

    def delete_retention_policy(self, log_group_name):
        if log_group_name not in self.groups:
            raise ResourceNotFoundException()
        log_group = self.groups[log_group_name]
        return log_group.set_retention_policy(None)

    def list_tags_log_group(self, log_group_name):
        if log_group_name not in self.groups:
            raise ResourceNotFoundException()
        log_group = self.groups[log_group_name]
        return log_group.list_tags()

    def tag_log_group(self, log_group_name, tags):
        if log_group_name not in self.groups:
            raise ResourceNotFoundException()
        log_group = self.groups[log_group_name]
        log_group.tag(tags)

    def untag_log_group(self, log_group_name, tags):
        if log_group_name not in self.groups:
            raise ResourceNotFoundException()
        log_group = self.groups[log_group_name]
        log_group.untag(tags)


logs_backends = {}
for region in Session().get_available_regions("logs"):
    logs_backends[region] = LogsBackend(region)
for region in Session().get_available_regions("logs", partition_name="aws-us-gov"):
    logs_backends[region] = LogsBackend(region)
for region in Session().get_available_regions("logs", partition_name="aws-cn"):
    logs_backends[region] = LogsBackend(region)
