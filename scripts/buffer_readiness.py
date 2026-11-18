#!/usr/bin/env python3
"""Read-only Buffer organization, Facebook, and Instagram channel discovery."""

from __future__ import annotations

from engine.buffer_publisher import BufferChannel, BufferClient, BufferPublisherError, load_buffer_config_from_env


def _safe_log_value(value: str) -> str:
    """Keep provider-supplied labels on one bounded log line."""
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())[:200]


def _channel_state(channel: BufferChannel) -> str:
    if channel.is_disconnected:
        return "DISCONNECTED"
    if channel.is_locked:
        return "LOCKED"
    return "CONNECTED"


def _print_channel(index: int, channel: BufferChannel) -> None:
    """Print safe, non-secret channel discovery fields only."""
    print(
        "BUFFER_CHANNEL "
        f"index={index} id={_safe_log_value(channel.id)} "
        f"service={_safe_log_value(channel.service)} "
        f"display_name={_safe_log_value(channel.display_name)} "
        f"descriptor={_safe_log_value(channel.descriptor)} "
        f"type={_safe_log_value(channel.channel_type)} "
        f"state={_channel_state(channel)}"
    )


def main() -> int:
    try:
        client = BufferClient(load_buffer_config_from_env())
        try:
            orgs = client.organizations()
            print("BUFFER_ALL_ORGANIZATIONS:", [(o.id, o.name) for o in orgs])
        except Exception as e:
            print("BUFFER_ORGS_FETCH_ERROR:", str(e))
        organization, channels = client.read_connected_channels()
    except BufferPublisherError as err:
        print(f"BUFFER_READINESS_FAILED code={err.code} err={err}")
        return 1
    print("BUFFER_AUTH=PASS")
    print(f"BUFFER_ORGANIZATION_ID={organization.id}")
    print(f"BUFFER_CHANNEL_COUNT={len(channels)}")
    for index, channel in enumerate(channels, start=1):
        _print_channel(index, channel)
    try:
        facebook = client.select_facebook_channel(channels)
        instagram = client.select_instagram_channel(channels)
    except BufferPublisherError as err:
        print(f"BUFFER_READINESS_FAILED code={err.code} err={err}")
        return 1
    print(f"BUFFER_FACEBOOK_CHANNEL_ID={facebook.id}")
    print(f"BUFFER_FACEBOOK_PAGE={_safe_log_value(facebook.display_name)}")
    print(f"BUFFER_FACEBOOK_SERVICE={_safe_log_value(facebook.service)}")
    print(f"BUFFER_FACEBOOK_DESCRIPTOR={_safe_log_value(facebook.descriptor)}")
    print(f"BUFFER_FACEBOOK_TYPE={_safe_log_value(facebook.channel_type)}")
    print(f"BUFFER_FACEBOOK_STATE={_channel_state(facebook)}")
    print(f"BUFFER_INSTAGRAM_CHANNEL_ID={instagram.id}")
    print(f"BUFFER_INSTAGRAM_ACCOUNT={_safe_log_value(instagram.display_name)}")
    print(f"BUFFER_INSTAGRAM_SERVICE={_safe_log_value(instagram.service)}")
    print(f"BUFFER_INSTAGRAM_DESCRIPTOR={_safe_log_value(instagram.descriptor)}")
    print(f"BUFFER_INSTAGRAM_TYPE={_safe_log_value(instagram.channel_type)}")
    print(f"BUFFER_INSTAGRAM_STATE={_channel_state(instagram)}")
    try:
        schema_query = """
        query IntrospectCreatePost {
          __type(name: "CreatePostInput") {
            name
            inputFields {
              name
              type {
                name
                kind
                ofType {
                  name
                  kind
                }
              }
            }
          }
        }
        """
        schema_res = client.graphql(schema_query)
        fields = schema_res.get("data", {}).get("__type", {}).get("inputFields", [])
        print("BUFFER_SCHEMA_CREATE_POST_INPUT_FIELDS:")
        for f in fields:
            t = f.get("type", {})
            t_str = t.get("name") or (t.get("ofType", {}).get("name") if t.get("ofType") else t.get("kind"))
            print(f"  {f.get('name')}: {t_str}")
    except Exception as exc:
        print(f"BUFFER_SCHEMA_INTROSPECTION_ERROR: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
