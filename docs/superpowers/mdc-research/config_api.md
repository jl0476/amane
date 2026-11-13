# MDC 配置 API（字段级）

> 探测日期 2026-11-12。全部端点 GET 返回 200 JSON，源码位于 `src/api/config/*.rs`。
> 敏感值（openai_key / proxy / webhook url / cookies）已脱敏为 `<REDACTED>`。

## `/api/config/common`

| 字段 | 值 |
|---|---|
| `delete_by_ext` | false |
| `delete_by_name` | false |
| `delete_by_size` | false |
| `delete_empty_parent_after_move` | false |
| `delete_metadata_folder_when_failed` | false |
| `extra_ext_whitelist` | [] |
| `file_ext_filter` | ["mp4", "avi", "rmvb", "wmv", "mov", "mkv", "webm", "iso", "mpg", "m4v", "ts", "flv", "strm", "vob",… |
| `file_name_blacklist` | [] |
| `file_sanitize_list` | ["2048论坛@fun2048.com", "1080p", "720p", "22-sht.me", "-HD", "bbs2048.org@", "hhd800.com@", "icao.me@… |
| `file_size_filter` | 104857600 |
| `link_mode` | 2 |
| `protect_ext_whitelist` | false |
| `target_folder` | "<媒体库目录>" |
| `target_folder_metadata` | "<媒体库目录>/<元数据子目录>" |
| `target_overwrite_pic` | false |
| `target_overwrite_video` | false |
| `uncensored_hack_keywords` | ["uncensored", "破解"] |
| `watch_dirs` | [] |
| `watcher_enabled` | false |
| `watcher_kind` | 1 |

## `/api/config/naming`

| 字段 | 值 |
|---|---|
| `category_mapping` | {"jp_censored": "日本有码", "jp_uncensored": "日本无码", "jp_amateur": "素人", "jp_fc2": "FC2", "cn": "国产", "e… |
| `category_rules` | [] |
| `file` | "{number}" |
| `folder` | "【{{ actor }}】{{ number }}{% if mosaic  != \"有码\" %}-{{ mosaic }}{% endif %}{% if subtitle != \"无字幕\… |
| `image_with_prefix` | true |
| `max_actor_count` | 3 |
| `max_folder_length` | 0 |
| `prefixes` | {"has_subtitle": "中字", "no_subtitle": "无字幕", "is_censored": "有码", "is_uncensored": "无码", "is_uncenso… |
| `suffix` | "{mosaic}{subtitle}" |
| `suffixs` | {"has_subtitle": "-C", "add_chs_to_subtitle": true, "is_censored": "", "is_uncensored": "", "is_unce… |
| `title` | "{actor} {number} {title}" |

## `/api/config/metadata`

| 字段 | 值 |
|---|---|
| `auto_translate_outline` | true |
| `auto_translate_title` | true |
| `deepl_api_key` | "" |
| `deepl_endpoint` | "" |
| `flaresolverr` | "http://<REDACTED>:8191/v1" |
| `image_strict_mode` | false |
| `openai_custom_prompt` | "" |
| `outline_trim_multiple_br` | true |
| `static_data_lang` | "zh_cn" |
| `strict_mode` | false |
| `translate_engine` | "OpenAI" |
| `use_actor_mapping` | true |
| `use_tag_mapping` | true |
| `use_title_from_sehuatang` | true |

## `/api/config/provider`

| 字段 | 值 |
|---|---|
| `configs` | {"Madou": {"enabled": true, "base_url": "<REDACTED len=17>", "cookies": "", "user_agent": "", "coold… |
| `default_retry_times` | 0 |
| `priorities` | {"jav_censored": ["dmm", "Mgstage", "Javlibrary", "avbase", "hbox_jp", "Javdb", "Javbus", "jav321", … |
| `priorities_user_modified` | false |
| `series_type_keywords` | {"jav_censored": [], "jav_uncensored": [], "jav_amateur": [], "cn": [], "ea": []} |
| `series_type_keywords_in_path` | {"jav_censored": [], "jav_uncensored": [], "jav_amateur": [], "cn": [], "ea": []} |

## `/api/config/download`

| 字段 | 值 |
|---|---|
| `crop_aspect` | "Full" |
| `crop_poster` | false |
| `crop_type_amateur` | "Face" |
| `crop_type_censored` | "Right" |
| `crop_type_cn` | "None" |
| `crop_type_fc2` | "Face" |
| `crop_type_uncensored` | "None" |
| `crop_when_poster_low_res` | true |
| `extra_fanarts` | false |
| `extra_fanarts_behind_scenes` | false |
| `fanart` | true |
| `hd_poster_amazon` | false |
| `hd_poster_amazon_strict` | false |
| `hd_poster_tenhow` | false |
| `poster` | true |
| `subtitles_folder` | null |
| `thumb` | true |
| `thumb_download_strategy` | "Priority" |
| `trailer` | false |

## `/api/config/nfo`

| 字段 | 值 |
|---|---|
| `add_part_to_title` | false |
| `add_translated_by` | false |
| `additional_tags` | {"serial_name": true, "actors": true, "resolution": true, "mosaic": true, "has_subtitle": true, "sub… |
| `enable_actor` | true |
| `enable_country` | true |
| `enable_cover` | true |
| `enable_criticrating` | true |
| `enable_customrating` | true |
| `enable_director` | true |
| `enable_genre` | true |
| `enable_label` | true |
| `enable_maker` | true |
| `enable_mpaa` | true |
| `enable_originalplot` | true |
| `enable_originaltitle` | true |
| `enable_outline` | true |
| `enable_plot` | true |
| `enable_poster` | true |
| `enable_premiered` | true |
| `enable_publisher` | true |
| `enable_release` | true |
| `enable_releasedate` | true |
| `enable_runtime` | true |
| `enable_score` | true |
| `enable_series` | true |
| `enable_sorttitle` | true |
| `enable_studio` | true |
| `enable_tag` | true |
| `enable_trailer` | true |
| `enable_votes` | true |
| `enable_website` | true |
| `enable_year` | true |
| `enabled` | true |
| `set_by_actress` | false |
| `set_by_serial_name` | false |
| `set_by_series` | true |
| `tagline_format` | "发行日期: {release}" |

## `/api/config/watermark`

| 字段 | 值 |
|---|---|
| `custom_folder` | null |
| `enabled` | true |
| `fanart` | false |
| `gap` | 0 |
| `mode` | "Clockwise" |
| `offset_x` | 0 |
| `offset_y` | 0 |
| `poster` | true |
| `resolution_style` | "Default" |
| `scale` | 9 |
| `start` | 0 |
| `style` | "OldRed" |
| `thumb` | true |
| `watermark_censored` | false |
| `watermark_censored_fixed` | 5 |
| `watermark_hack` | true |
| `watermark_hack_fixed` | 5 |
| `watermark_leak` | true |
| `watermark_leak_fixed` | 5 |
| `watermark_resolution` | true |
| `watermark_resolution_fixed` | 5 |
| `watermark_subtitle` | true |
| `watermark_subtitle_fixed` | 5 |
| `watermark_uncensored` | true |
| `watermark_uncensored_fixed` | 5 |

## `/api/config/webhook`

| 字段 | 值 |
|---|---|
| `enabled` | true |
| `endpoints` | [{"name": "feishu", "url": "<REDACTED len=17>", "method": "POST", "headers": {}, "on": ["failed"], "… |

## `/api/config/system`

| 字段 | 值 |
|---|---|
| `openai_base` | "<REDACTED len=24>" |
| `openai_key` | "<REDACTED len=17>" |
| `openai_model` | "<REDACTED len=25>" |
| `scrape_thread_count` | 1 |
| `scrape_timeout` | 600 |

## `/api/config/proxy`

| 字段 | 值 |
|---|---|
| `proxy` | "<REDACTED len=25>" |
| `timeout` | 60 |
| `timeout_trailer` | 120 |

## `/api/config/actress`

| 字段 | 值 |
|---|---|
| `emby_config` | {"server_address": "", "api_key": "", "user_id": null, "scheduled_refresh": false, "polling_last_day… |
| `enabled` | false |
| `overwrite_mode` | 0 |
| `scrape_info` | false |
| `scrape_pics` | false |

