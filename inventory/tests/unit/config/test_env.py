from config.env import get_settings


def test_redis_use_tls_defaults_to_false():
    # The CDK stack provisions a plain AWS::ElastiCache::CacheCluster,
    # which has no in-transit encryption support — a True default here
    # would open an SSL connection against a server that never speaks
    # TLS, silently breaking the cache on every deploy.
    settings = get_settings()

    assert settings.redis_use_tls is False
