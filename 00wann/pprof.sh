#!/bin/bash
go tool pprof -http=0.0.0.0:8001 $@
