#!/bin/bash
pkill -f "rf_bridge" 2>/dev/null && echo "stopped RF_Bridge" || echo "RF_Bridge not running"
pkill -f "hackrf_web" 2>/dev/null && echo "stopped hackrf_web" || echo "hackrf_web not running"
pkill -9 -f "gps-sdr-sim" 2>/dev/null && echo "killed gps-sdr-sim" || true
pkill -9 -f "hackrf_transfer" 2>/dev/null && echo "killed hackrf_transfer" || true
pkill -9 -f "hackrf_sweep" 2>/dev/null && echo "killed hackrf_sweep" || true
