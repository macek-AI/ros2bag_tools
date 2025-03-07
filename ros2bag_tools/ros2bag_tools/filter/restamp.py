# Copyright 2022 AIT Austrian Institute of Technology GmbH
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import re

from rclpy.serialization import serialize_message
from rclpy.time import Duration
from rclpy.time import Time

from ros2bag_tools.filter import FilterExtension, FilterResult
from ros2bag_tools.logging import warn_once
from ros2bag_tools.reader import TopicDeserializer

from tf2_msgs.msg import TFMessage
from visualization_msgs.msg import MarkerArray


def nanoseconds_duration(data: str):
    try:
        val = int(data)
    except ValueError:
        val = int(float(data) * 10**9)
    return Duration(nanoseconds=val)


def set_header_stamp(msg, t: int):
    t_ros = Time(nanoseconds=t)
    if hasattr(msg, 'header'):
        msg.header.stamp = t_ros.to_msg()
    elif isinstance(msg, TFMessage):
        for transform in msg.transforms:
            transform.header.stamp = t_ros.to_msg()
    elif isinstance(msg, MarkerArray) and msg.markers:
        for marker in msg.markers:
            marker.header.stamp = t_ros.to_msg()
    return msg


def t_from_header(msg):
    if hasattr(msg, 'header'):
        header_time = Time.from_msg(msg.header.stamp)
        return header_time.nanoseconds
    elif isinstance(msg, TFMessage):
        times = [Time.from_msg(
            transform.header.stamp).nanoseconds for transform in msg.transforms]
        if len(times) > 0:
            return min(times)
    elif isinstance(msg, MarkerArray) and msg.markers:
        times = [Time.from_msg(
            marker.header.stamp).nanoseconds for marker in msg.markers]
        if len(times) > 0:
            return min(times)
    return None


class RestampFilter(FilterExtension):

    def __init__(self):
        super().__init__()
        self._deserializer = TopicDeserializer()
        self._delayed_msgs = []
        self._min_valid_time = None

    def add_arguments(self, parser):
        parser.add_argument(
            '-i', '--invert', action='store_true',
            help='apply filter in reverse by setting header stamps to bag message timestamps')
        parser.add_argument(
            '-u', '--offset-topic', nargs='+', default=[],
            help='topics to restamp with offset as regex string')
        parser.add_argument(
            '-c', '--offset', default='0', type=nanoseconds_duration,
            help='constant offset value in seconds (float) or nanoseconds (int) applied to offset'
                 ' topics')
        parser.add_argument(
            '--offset-header', action='store_true',
            help='apply offset to header if offset is enabled')

    def set_args(self, metadatas, args):
        self._invert = args.invert
        self._offset_topics = set()
        for metadata in metadatas:
            for topic in metadata.topics_with_message_count:
                topic_name = topic.topic_metadata.name
                if any((re.match(r, topic_name) for r in args.offset_topic)):
                    self._offset_topics.add(topic_name)
        self._offset = args.offset
        self._offset_header = args.offset_header

    def filter_topic(self, topic_metadata):
        self._deserializer.add_topic(topic_metadata)
        return topic_metadata

    def _add_header_offset(self, msg):
        if hasattr(msg, 'header'):
            t_header = Time.from_msg(msg.header.stamp)
            t_header += self._offset
            msg.header.stamp = t_header.to_msg()
        elif isinstance(msg, TFMessage):
            for transform in msg.transforms:
                t_header = Time.from_msg(transform.header.stamp)
                t_header += self._offset
                transform.header.stamp = t_header.to_msg()
        elif isinstance(msg, MarkerArray) and msg.markers:
            for marker in msg.markers:
                t_header = Time.from_msg(marker.header.stamp)
                t_header += self._offset
                marker.header.stamp = t_header.to_msg()
        return msg

    def filter_msg(self, serialized_msg):
        (topic, data, t) = serialized_msg
        msg = self._deserializer.deserialize(topic, data)

        if self._invert:
            msg = set_header_stamp(msg, t)
        else:
            new_t = t_from_header(msg)
            if new_t is None:
                warn_once(self._logger,
                          f'{topic} has no header, using bag timestamp instead')
            else:
                t = new_t
                if new_t > 0:
                    if self._min_valid_time is None or new_t < self._min_valid_time:
                        self._min_valid_time = new_t
                else:
                    if isinstance(msg, TFMessage):
                        warn_once(self._logger,
                                f"Delaying message from {topic} with timestamp {new_t}.")
                        self._delayed_msgs.append((topic, msg, data, t))
                    return FilterResult.DROP_MESSAGE

        if topic in self._offset_topics:
            t += self._offset.nanoseconds

        if self._offset_header and topic in self._offset_topics:
            msg = self._add_header_offset(msg)

        data = serialize_message(msg)

        if len(self._delayed_msgs) == 0:
            return (topic, data, t)
        else:
            delayed_messages = self._process_delayed_messages()
            return [(topic, data, t)] + delayed_messages

    def _process_delayed_messages(self):
        if self._min_valid_time is None:
            return []

        processed_msgs = []
        for topic, msg, data, t in self._delayed_msgs:
            warn_once(self._logger,
                      f"Assigning minimum valid timestamp {self._min_valid_time} to {topic} message.")
            new_t = self._min_valid_time

            if topic in self._offset_topics:
                new_t += self._offset.nanoseconds

            if self._offset_header and topic in self._offset_topics:
                msg = self._add_header_offset(msg)

            data = serialize_message(msg)
            processed_msgs.append((topic, data, new_t))

        self._delayed_msgs = []

        return processed_msgs
