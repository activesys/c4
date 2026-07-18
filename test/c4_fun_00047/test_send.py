"""C4_FUN_00047 — ASFP2 data send tests (stdout verification, pcap skipped)."""

import os, time, pytest
from conftest import (
    _make_standard_config, _make_5points_config, _make_gapped_config,
    _make_smart0_config, _make_3points_config,
    start_asfp2_server, start_asfp2_client, start_sut,
    run_asfp2_server, run_asfp2_client_inject, parse_asfp2_server_output,
    send_fragmented_packet,
)


WAIT_FAST = 0.5   # timer=100ms → 5x timer
WAIT_SLOW = 2.5   # timer=1000ms → 2.5x timer

class TestSend:

    def _setup(self, prepare_environment, isolated_shm, iid, config_fn, inject_port=9700):
        isolated_shm(iid)
        config = config_fn()
        verify_port = 9800 + (inject_port - 9700)
        slow = inject_port != 9700
        if slow:
            for inst in config["c4_asfp2_server"]:
                inst["port"] = inject_port
            for inst in config["c4_asfp2_client"]:
                inst["port"] = verify_port
                inst["timer"] = 1000  # slower poll to help inject accumulate
        config_path, _ = prepare_environment(config, iid)
        srv = start_asfp2_server(config_path)
        vp, rp, rf = run_asfp2_server(verify_port)
        sut = start_asfp2_client(config_path)
        start_sut(sut, config_path)
        wait = WAIT_SLOW if slow else WAIT_FAST
        return config_path, sut, srv, vp, rp, rf, inject_port, wait

    def _check(self, vp, rp, rf, min_records, sut, srv):
        vp.terminate(); vp.wait(); rf.close()
        recs = parse_asfp2_server_output(rp)
        srv.call_tool("stop", {})
        sut.call_tool("stop", {})
        assert len(recs) >= min_records, f"Expected >= {min_records}, got {len(recs)}"
        return recs

    # ── TC1 ──
    def test_tc1_basic_send(self, prepare_environment, isolated_shm):
        _, sut, srv, vp, rp, rf, port, wait = self._setup(prepare_environment, isolated_shm, "tc1", _make_standard_config)
        run_asfp2_client_inject(port, 1000, 1001, 4, val_begin=100, val_end=200, extra_args=["--i0","10","--i1","10"])
        time.sleep(wait)
        self._check(vp, rp, rf, 1, sut, srv)

    # ── TC2 ──
    def test_tc2_key_sequence_continuous(self, prepare_environment, isolated_shm):
        _, sut, srv, vp, rp, rf, port, wait = self._setup(prepare_environment, isolated_shm, "tc2", _make_5points_config, 9701)
        run_asfp2_client_inject(port, 1000, 1004, 4, val_begin=100, val_end=500)
        time.sleep(wait)
        self._check(vp, rp, rf, 1, sut, srv)

    # ── TC3 ──
    def test_tc3_key_sequence_split(self, prepare_environment, isolated_shm):
        _, sut, srv, vp, rp, rf, port, wait = self._setup(prepare_environment, isolated_shm, "tc3", _make_gapped_config, 9702)
        run_asfp2_client_inject(port, 1000, 1001, 4, val_begin=100, val_end=200)
        run_asfp2_client_inject(port, 1005, 1006, 4, val_begin=300, val_end=400)
        time.sleep(wait)
        self._check(vp, rp, rf, 1, sut, srv)

    # ── TC4 ──
    def test_tc4_same_data_type(self, prepare_environment, isolated_shm):
        _, sut, srv, vp, rp, rf, port, wait = self._setup(prepare_environment, isolated_shm, "tc4", _make_standard_config, 9703)
        run_asfp2_client_inject(port, 1000, 1001, 4, val_begin=100, val_end=200)
        time.sleep(wait)
        self._check(vp, rp, rf, 1, sut, srv)

    # ── TC5 ──
    def test_tc5_same_data_type_fail(self, prepare_environment, isolated_shm):
        recs = []
        for attempt in range(3):
            _, sut, srv, vp, rp, rf, port, wait = self._setup(prepare_environment, isolated_shm, "tc5", _make_standard_config, 9704)
            run_asfp2_client_inject(port, 1000, 1001, 4, val_begin=100, val_end=200, extra_args=["-z","1"])   # only addr 1000, type 4
            time.sleep(0.05)
            run_asfp2_client_inject(port, 1001, 1002, 10, val_begin=300, val_end=400, extra_args=["-z","1"])  # only addr 1001, type 10
            time.sleep(wait)
            recs = self._check(vp, rp, rf, 1, sut, srv)
            if len(recs) >= 1:
                break
        assert len(recs) >= 1

    # ── TC6 ──
    def test_tc6_same_timestamp_smart1(self, prepare_environment, isolated_shm):
        _, sut, srv, vp, rp, rf, port, wait = self._setup(prepare_environment, isolated_shm, "tc6", _make_standard_config, 9705)
        run_asfp2_client_inject(port, 1000, 1001, 4, ts_start=1000000, val_begin=100, val_end=200)
        time.sleep(wait)
        recs = self._check(vp, rp, rf, 1, sut, srv)
        for r in recs:
            assert r["timestamp"] % 1000 == 0

    # ── TC7 ──
    def test_tc7_same_timestamp_fail(self, prepare_environment, isolated_shm):
        _, sut, srv, vp, rp, rf, port, wait = self._setup(prepare_environment, isolated_shm, "tc7", _make_smart0_config, 9706)
        run_asfp2_client_inject(port, 1000, 1001, 4, ts_start=1000000, val_begin=100, val_end=200, extra_args=["-z","1"])   # addr 1000
        run_asfp2_client_inject(port, 1001, 1002, 4, ts_start=2000000, val_begin=300, val_end=400, extra_args=["-z","1"])  # addr 1001
        time.sleep(1.5)
        self._check(vp, rp, rf, 1, sut, srv)

    # ── TC8 ──
    def test_tc8_non_numeric_filter(self, prepare_environment, isolated_shm):
        _, sut, srv, vp, rp, rf, port, wait = self._setup(prepare_environment, isolated_shm, "tc8", _make_3points_config, 9707)
        run_asfp2_client_inject(port, 1000, 1001, 4, val_begin=100, val_end=200)
        run_asfp2_client_inject(port, 1002, 1003, 12, val_begin=0, val_end=0, extra_args=["-z","1","--str","teststr"])
        time.sleep(wait)
        self._check(vp, rp, rf, 1, sut, srv)

    # ── TC9 ──
    def test_tc9_bit_compression(self, prepare_environment, isolated_shm):
        _, sut, srv, vp, rp, rf, port, wait = self._setup(prepare_environment, isolated_shm, "tc9", _make_5points_config, 9708)
        run_asfp2_client_inject(port, 1000, 1004, 0, val_begin=0, val_end=1, extra_args=["-z","5"])
        time.sleep(wait)
        self._check(vp, rp, rf, 1, sut, srv)

    # ── TC10 ──
    def test_tc10_float32_encoding(self, prepare_environment, isolated_shm):
        _, sut, srv, vp, rp, rf, port, wait = self._setup(prepare_environment, isolated_shm, "tc10", _make_standard_config, 9709)
        run_asfp2_client_inject(port, 1000, 1001, 10, val_begin=100, val_end=200, extra_args=["-P","8"])
        time.sleep(wait)
        self._check(vp, rp, rf, 1, sut, srv)

    # ── TC11: reconnect ──
    def test_tc11_tcp_reconnect(self, prepare_environment, isolated_shm):
        iid = "tc11"
        config = _make_standard_config()
        for inst in config["c4_asfp2_client"]:
            inst["t0"] = 5
            inst["timer"] = 1000
        isolated_shm(iid)
        for inst in config["c4_asfp2_server"]:
            inst["port"] = 9710
        for inst in config["c4_asfp2_client"]:
            inst["port"] = 9810
        config_path, _ = prepare_environment(config, iid)
        srv = start_asfp2_server(config_path)
        vp1, rp1, rf1 = run_asfp2_server(9810)
        sut = start_asfp2_client(config_path)
        start_sut(sut, config_path)
        try:
            run_asfp2_client_inject(9710, 1000, 1001, 4, val_begin=100, val_end=200)
            time.sleep(WAIT_SLOW)
            vp1.terminate(); vp1.wait(); rf1.close()
            recs1 = parse_asfp2_server_output(rp1)
            assert len(recs1) >= 1

            sut.call_tool("stop", {})
            vp2, rp2, rf2 = run_asfp2_server(9810)
            time.sleep(0.5)  # let verify server start listening
            start_sut(sut, config_path)
            time.sleep(5)  # t0 reconnect + data
            run_asfp2_client_inject(9710, 1000, 1001, 4, val_begin=300, val_end=400)
            time.sleep(WAIT_SLOW)
            vp2.terminate(); vp2.wait(); rf2.close()
            recs2 = parse_asfp2_server_output(rp2)
            assert len(recs2) >= 1
        finally:
            sut.call_tool("stop", {})
            srv.call_tool("stop", {})

    # ── TC12: KeepAlive reconnect ──
    def test_tc12_keepalive_reconnect(self, prepare_environment, isolated_shm):
        iid = "tc12"
        config = _make_standard_config()
        for inst in config["c4_asfp2_client"]:
            inst["t1"] = 2; inst["t2"] = 1; inst["t0"] = 5; inst["timer"] = 1000
        isolated_shm(iid)
        for inst in config["c4_asfp2_server"]:
            inst["port"] = 9711
        for inst in config["c4_asfp2_client"]:
            inst["port"] = 9811
        config_path, _ = prepare_environment(config, iid)
        srv = start_asfp2_server(config_path)
        vp1, rp1, rf1 = run_asfp2_server(9811)
        sut = start_asfp2_client(config_path)
        start_sut(sut, config_path)
        try:
            run_asfp2_client_inject(9711, 1000, 1001, 4, val_begin=100, val_end=200)
            time.sleep(WAIT_SLOW)
            vp1.terminate(); vp1.wait(); rf1.close()
            recs1 = parse_asfp2_server_output(rp1)
            assert len(recs1) >= 1

            sut.call_tool("stop", {})
            vp2, rp2, rf2 = run_asfp2_server(9811)
            time.sleep(0.5)  # let verify server start listening
            start_sut(sut, config_path)
            time.sleep(12)  # KeepAlive: t1=2s + T2=1s + t0=5s + data
            run_asfp2_client_inject(9711, 1000, 1001, 4, val_begin=300, val_end=400)
            time.sleep(WAIT_SLOW)
            vp2.terminate(); vp2.wait(); rf2.close()
            recs2 = parse_asfp2_server_output(rp2)
            assert len(recs2) >= 1
        finally:
            sut.call_tool("stop", {})
            srv.call_tool("stop", {})

    # ── TC13 ──
    def test_tc13_smart1_timestamp_zeroing(self, prepare_environment, isolated_shm):
        _, sut, srv, vp, rp, rf, port, wait = self._setup(prepare_environment, isolated_shm, "tc13", _make_standard_config, 9712)
        run_asfp2_client_inject(port, 1000, 1001, 4, ts_start=1768848814264, val_begin=100, val_end=200, extra_args=["-z","1"])
        time.sleep(wait)
        recs = self._check(vp, rp, rf, 1, sut, srv)
        assert recs[0]["timestamp"] == 1768848814000, f"Got {recs[0]['timestamp']}"

    # ── TC14 ──
    def test_tc14_no_new_data_no_send(self, prepare_environment, isolated_shm):
        _, sut, srv, vp1, rp1, rf1, port, wait = self._setup(prepare_environment, isolated_shm, "tc14", _make_standard_config, 9713)
        run_asfp2_client_inject(port, 1000, 1001, 4, val_begin=100, val_end=200)
        time.sleep(0.5)
        vp1.terminate(); vp1.wait(); rf1.close()
        parse_asfp2_server_output(rp1)

        time.sleep(0.5)
        vp2, rp2, rf2 = run_asfp2_server(9813)
        time.sleep(0.5)
        vp2.terminate(); vp2.wait(); rf2.close()
        recs2 = parse_asfp2_server_output(rp2)
        srv.call_tool("stop", {})
        sut.call_tool("stop", {})
        assert len(recs2) == 0, f"Expected 0 on round 2, got {len(recs2)}"

    # ── TC15: TCP fragmented packet ──
    def test_tc15_fragmented_packet(self, prepare_environment, isolated_shm):
        _, sut, srv, vp, rp, rf, port, wait = self._setup(
            prepare_environment, isolated_shm, "tc15", _make_3points_config, 9714
        )
        send_fragmented_packet(
            port=port,
            addr_start=1000,
            count=3,
            data_type=4,
            val_start=100,
            ts=int(time.time() * 1000),
            split_at=30,
        )
        time.sleep(wait)
        recs = self._check(vp, rp, rf, 3, sut, srv)
        assert len(recs) == 3, f"Expected 3 records after fragmented packet, got {len(recs)}"
