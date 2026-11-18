CXX ?= g++
CXXFLAGS ?= -O3 -march=native -std=c++20 -Wall -Wextra -Werror -fno-math-errno -fopenmp-simd
PW_CFLAGS := $(shell pkg-config --cflags libpipewire-0.3)
PW_LIBS := $(shell pkg-config --libs libpipewire-0.3)

.PHONY: all prepare setup install
all: build/nhmc-native

build:
	mkdir -p build

build/nhmc-native: native/nhmc_native.cpp | build
	$(CXX) $(CXXFLAGS) -Wno-missing-field-initializers $(PW_CFLAGS) -o $@ $< $(PW_LIBS)

prepare:
	python3 prepare.py

setup:
	python3 setup.py

install: all
	python3 install.py

