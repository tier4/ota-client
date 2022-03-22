PB2_OUT_BASE_DIR := pb2
PB2_OUT_DIR := $(PB2_OUT_BASE_DIR)/otaclient_pb2

.PHONY: all
all: whl

.PHONY: clean
clean:
	rm -rf build *.egg-info dist $(PB2_OUT_DIR)
	rm -rf pb2/build pb2/*.egg-info pb2/dist

.PHONY: build
build:
	rm -rf build $(PB2_OUT_DIR)
	mkdir -p $(PB2_OUT_DIR)/v2
	cp -a ./otaclient_v2.proto $(PB2_OUT_DIR)/v2/otaclient.proto
	python3 -m grpc_tools.protoc -I$(PB2_OUT_BASE_DIR) --python_out=$(PB2_OUT_BASE_DIR) --grpc_python_out=$(PB2_OUT_BASE_DIR) ./$(PB2_OUT_DIR)/v2/otaclient.proto
	for dir in $$(find $(PB2_OUT_DIR) -type d); do touch $${dir}/__init__.py; done

.PHONY: install
install:
	python3 -m pip install --force-reinstall $$(ls -rv ./whl/otaclient_pb2-*-py3-none-any.whl | head -n 1)

.PHONY: whl
whl: build
	python3 -m pip install --upgrade pip setuptools wheel
	cd pb2; rm -rf build *.egg-info dist *.whl; mkdir -p ../whl
	cd pb2; python3 setup.py bdist_wheel; mv dist/*.whl ../whl/
