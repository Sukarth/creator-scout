"""Shrink saved API responses for use as test fixtures.

Drops media URLs, avatars, music and tracking payloads that the parsers never
read, keeping every field the pipeline uses. Run after saving a new fixture:

    python tests/prune_fixtures.py tests/fixtures/*.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

DROP_KEYS = {
    "video", "music", "added_sound_music_info", "share_info", "image_post_info", "anchors",
    "anchors_extras", "interaction_stickers", "log_pb", "extra", "global_doodle_config",
    "music_list", "challenge_list", "effects", "musics", "mix_list", "challenges", "items",
    "item_list", "risk_infos", "text_extra", "video_labels", "cha_list", "status",
    "green_screen_materials", "aweme_acl", "comment_config", "creation_info", "content_desc_extra",
    "commerce_config_data", "cover_labels", "platform_sync_info", "shield_edit_field_info",
    "advanced_feature_info", "user_now_pack_info", "follow_up_item_id_groups", "search_highlight",
    "followers_detail", "account_labels", "events", "banners", "bottom_products", "mask_infos",
    "main_arch_common", "aigc_info", "ai_remix_info", "c2pa_info", "cc_template_info",
    "user_profile_guide", "homepage_bottom_toast", "user_spark_info", "can_message_follow_status_list",
    "mutual_relation_avatars", "relative_users", "user_tags", "type_label", "fake_data_info",
    "shield_comment_notice", "profileTab", "eventList", "suggestAccountBind",
}


def prune(node):
    if isinstance(node, dict):
        return {k: prune(v) for k, v in node.items()
                if k not in DROP_KEYS and not k.startswith(("avatar", "cover", "ad_cover",
                                                            "white_cover", "share_qrcode"))
                and not (isinstance(v, dict) and "url_list" in v)}
    if isinstance(node, list):
        return [prune(v) for v in node]
    return node


def main(paths: list[str]) -> None:
    for p in map(Path, paths):
        data = json.loads(p.read_text(encoding="utf-8"))
        before = p.stat().st_size
        p.write_text(json.dumps(prune(data), ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{p.name}: {before // 1024} KB -> {p.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main(sys.argv[1:])
