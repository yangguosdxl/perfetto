#!/usr/bin/env python3
# Copyright (C) 2026 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import sys


GLOBAL_HEADER = b"!<arch>\n"


def _read_response_file(arg):
  if not arg.startswith("@"):
    return [arg]
  with open(arg[1:], "r", encoding="utf-8") as f:
    return f.read().split()


def _format_field(value, width):
  data = str(value).encode("ascii")
  if len(data) > width:
    raise ValueError("ar 字段过长: %r" % value)
  return data + b" " * (width - len(data))


def _write_member(out, name, data):
  header = b"".join([
      _format_field(name, 16),
      _format_field(0, 12),      # 确定性归档：时间戳固定为 0。
      _format_field(0, 6),       # 确定性归档：uid 固定为 0。
      _format_field(0, 6),       # 确定性归档：gid 固定为 0。
      _format_field("100644", 8),
      _format_field(len(data), 10),
      b"`\n",
  ])
  out.write(header)
  out.write(data)
  if len(data) % 2:
    out.write(b"\n")


def _build_string_table(member_names):
  table = bytearray()
  offsets = {}
  for name in member_names:
    if len(name) <= 15:
      continue
    offsets[name] = len(table)
    table.extend(name.encode("utf-8"))
    table.extend(b"/\n")
  return bytes(table), offsets


def create_archive(output, inputs):
  member_names = [os.path.basename(path) for path in inputs]
  string_table, long_name_offsets = _build_string_table(member_names)

  with open(output, "wb") as out:
    out.write(GLOBAL_HEADER)
    if string_table:
      _write_member(out, "//", string_table)

    for path, member_name in zip(inputs, member_names):
      if member_name in long_name_offsets:
        ar_name = "/%d" % long_name_offsets[member_name]
      else:
        ar_name = member_name + "/"
      with open(path, "rb") as f:
        _write_member(out, ar_name, f.read())


def main(argv):
  # 支持 llvm-ar rcsD output @rsp 的调用形态；flags 只用于兼容命令行。
  if len(argv) < 4:
    raise SystemExit("用法: ar.py rcsD <output> <inputs...|@rsp>")

  output = argv[2]
  inputs = []
  for arg in argv[3:]:
    inputs.extend(_read_response_file(arg))

  create_archive(output, inputs)


if __name__ == "__main__":
  main(sys.argv)
