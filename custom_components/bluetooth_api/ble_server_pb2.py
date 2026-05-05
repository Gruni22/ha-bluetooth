"""Runtime-built protobuf message classes for the ble_server bridge.

These mirror the messages added to esphome/components/api/api.proto:

    message SubscribeBleServerFramesRequest {}                 // id 149
    message BleServerFrameResponse  { bytes data = 1; }        // id 150
    message BleServerSendFrameRequest { bytes data = 1; }      // id 151

We build them via FileDescriptorProto at import time (instead of shipping a
protoc-generated _pb2.py) so we are not tied to a specific protobuf gencode
version. aioesphomeapi pins protobuf at runtime; whatever version HA ships is
fine because we only use the stable runtime API (DescriptorPool +
MessageFactory).
"""

from __future__ import annotations

from google.protobuf import descriptor_pb2, descriptor_pool
from google.protobuf import message_factory


def _build_messages() -> tuple[type, type, type]:
    file_proto = descriptor_pb2.FileDescriptorProto()
    file_proto.name = "bluetooth_api/ble_server.proto"
    file_proto.package = "bluetooth_api"
    file_proto.syntax = "proto3"

    file_proto.message_type.add(name="SubscribeBleServerFramesRequest")

    frame_resp = file_proto.message_type.add(name="BleServerFrameResponse")
    f = frame_resp.field.add(name="data", number=1, type=descriptor_pb2.FieldDescriptorProto.TYPE_BYTES)
    f.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL

    send_req = file_proto.message_type.add(name="BleServerSendFrameRequest")
    f = send_req.field.add(name="data", number=1, type=descriptor_pb2.FieldDescriptorProto.TYPE_BYTES)
    f.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL

    pool = descriptor_pool.DescriptorPool()
    file_desc = pool.Add(file_proto)
    sub_cls = message_factory.GetMessageClass(file_desc.message_types_by_name["SubscribeBleServerFramesRequest"])
    resp_cls = message_factory.GetMessageClass(file_desc.message_types_by_name["BleServerFrameResponse"])
    send_cls = message_factory.GetMessageClass(file_desc.message_types_by_name["BleServerSendFrameRequest"])
    return sub_cls, resp_cls, send_cls


SubscribeBleServerFramesRequest, BleServerFrameResponse, BleServerSendFrameRequest = _build_messages()
