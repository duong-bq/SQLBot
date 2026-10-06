"""Test dựng client Redis theo REDIS_MODE: standalone, sentinel và cluster.

Chỉ kiểm tra cách dựng client và cấu hình; không test nào mở kết nối tới Redis thật.
"""

import pytest
from redis.asyncio import Redis
from redis.asyncio.cluster import RedisCluster
from redis.asyncio.sentinel import SentinelConnectionPool

from common.core.config import Settings, settings
from common.core.sqlbot_cache import build_redis_client, parse_redis_nodes


def test_parse_nodes_defaults_port_and_skips_blanks():
    assert parse_redis_nodes("a:26379, b ,,c:7000") == [("a", 26379), ("b", 6379), ("c", 7000)]


def test_standalone_uses_first_node_database_and_password(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_MODE", "standalone")
    monkeypatch.setattr(settings, "REDIS_CONNECTION_STRING", "cache-host:6380")
    monkeypatch.setattr(settings, "REDIS_PASSWORD", "secret")
    monkeypatch.setattr(settings, "REDIS_DATABASE_ID", 3)
    client = build_redis_client()
    assert isinstance(client, Redis)
    kwargs = client.connection_pool.connection_kwargs
    assert (kwargs["host"], kwargs["port"], kwargs["db"], kwargs["password"]) == ("cache-host", 6380, 3, "secret")


def test_standalone_defaults_to_localhost_and_sends_no_password(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_MODE", "standalone")
    monkeypatch.setattr(settings, "REDIS_CONNECTION_STRING", "")
    monkeypatch.setattr(settings, "REDIS_PASSWORD", "")
    kwargs = build_redis_client().connection_pool.connection_kwargs
    assert (kwargs["host"], kwargs["port"], kwargs["password"]) == ("localhost", 6379, None)


def test_sentinel_follows_master_through_sentinels(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_MODE", "sentinel")
    monkeypatch.setattr(settings, "REDIS_CONNECTION_STRING", "s1:26379,s2:26379")
    monkeypatch.setattr(settings, "REDIS_SENTINEL_MASTER", "mymaster")
    monkeypatch.setattr(settings, "REDIS_PASSWORD", "data-pw")
    monkeypatch.setattr(settings, "REDIS_SENTINEL_PASSWORD", "sentinel-pw")
    monkeypatch.setattr(settings, "REDIS_DATABASE_ID", 2)
    client = build_redis_client()
    pool = client.connection_pool
    assert isinstance(client, Redis) and isinstance(pool, SentinelConnectionPool)
    assert pool.service_name == "mymaster"
    assert [(s.connection_pool.connection_kwargs["host"], s.connection_pool.connection_kwargs["port"])
            for s in pool.sentinel_manager.sentinels] == [("s1", 26379), ("s2", 26379)]
    assert pool.sentinel_manager.sentinels[0].connection_pool.connection_kwargs["password"] == "sentinel-pw"
    assert pool.connection_kwargs["password"] == "data-pw"
    assert pool.connection_kwargs["db"] == 2


def test_cluster_uses_all_seed_nodes(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_MODE", "cluster")
    monkeypatch.setattr(settings, "REDIS_CONNECTION_STRING", "n1:7000,n2:7001")
    monkeypatch.setattr(settings, "REDIS_PASSWORD", "data-pw")
    client = build_redis_client()
    assert isinstance(client, RedisCluster)
    seeds = client.nodes_manager.startup_nodes.values()
    assert sorted((n.host, n.port) for n in seeds) == [("n1", 7000), ("n2", 7001)]


@pytest.mark.parametrize(
    "env, message",
    [
        ({"REDIS_MODE": "sentinel", "REDIS_SENTINEL_MASTER": "m"}, "REDIS_CONNECTION_STRING"),
        ({"REDIS_MODE": "sentinel", "REDIS_CONNECTION_STRING": "s1:26379"}, "REDIS_SENTINEL_MASTER"),
        ({"REDIS_MODE": "cluster"}, "REDIS_CONNECTION_STRING"),
    ],
)
def test_incomplete_sentinel_or_cluster_config_fails_at_startup(env, message):
    with pytest.raises(ValueError, match=message):
        Settings(_env_file=None, CACHE_TYPE="redis", **env)


def test_memory_cache_ignores_redis_mode():
    Settings(_env_file=None, CACHE_TYPE="memory", REDIS_MODE="cluster")
