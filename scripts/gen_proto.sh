#!/usr/bin/env sh
# Generation command for local development (Docker does the same in its build stage).
set -e
cd "$(dirname "$0")/.."
mkdir -p gen
python -m grpc_tools.protoc -I proto --python_out=gen --grpc_python_out=gen proto/veritas/exam/v1/exam.proto
echo "generated gen/veritas/exam/v1/exam_pb2.py and exam_pb2_grpc.py"
