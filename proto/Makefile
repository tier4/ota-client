PB2_OUT_BASE_DIR := pb2
PB2_OUT_DIR := $(PB2_OUT_BASE_DIR)/otaclient_pb2

DEPS = $(PB2_OUT_DIR)/v2/otaclient.proto \
       $(PB2_OUT_DIR)/v2/otaclient_pb2_grpc.py \
       $(PB2_OUT_DIR)/v2/otaclient_pb2.py \
       $(PB2_OUT_DIR)/v2/__init__.py \
       $(PB2_OUT_DIR)/__init__.py

.PHONY: all
all: setup whl

$(PB2_OUT_DIR)/v2/otaclient.proto: otaclient_v2.proto
	cp -a $< $@

$(PB2_OUT_DIR)/v2/otaclient_pb2_grpc.py $(PB2_OUT_DIR)/v2/otaclient_pb2.py: $(PB2_OUT_DIR)/v2/otaclient.proto
	python3 -m grpc_tools.protoc \
		-I$(PB2_OUT_BASE_DIR) \
		--python_out=$(PB2_OUT_BASE_DIR) \
		--grpc_python_out=$(PB2_OUT_BASE_DIR) \
		./$(PB2_OUT_DIR)/v2/otaclient.proto

$(PB2_OUT_DIR)/__init__.py:
	touch $@

$(PB2_OUT_DIR)/v2/__init__.py:
	touch $@

.PHONY: setup
setup:
	python3 -m pip install --upgrade pip
	python3 -m pip install --upgrade setuptools wheel
	python3 -m pip install --upgrade grpcio-tools

.PHONY: whl
whl: $(DEPS)
	cd pb2; \
	python3 setup.py bdist_wheel;

.PHONY: clean
clean:
	rm -rf pb2/dist pb2/build/ pb2/otaclient_pb2*
