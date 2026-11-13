# MDC 刮削源配置（`/api/config/provider`）

共 21 个源。

| 源 ID | enabled | base_url | cookies | user_agent | cooldown | retry | proxy | api_key |
|---|---|---|---|---|---|---|---|---|
| `Carib` | True | <REDACTED len=28> | — | — | 0 | 0 | — | — |
| `Fc2` | True | <REDACTED len=30> | — | — | 0 | 0 | — | — |
| `Javbus` | True | <REDACTED len=23> | 有 | 有 | 0 | 0 | — | — |
| `Javdb` | True | <REDACTED len=20> | 有 | 有 | 10 | 0 | — | — |
| `Javlibrary` | True | <REDACTED len=20> | 有 | 有 | 0 | 0 | 有 | — |
| `Madou` | True | <REDACTED len=18> | — | — | 0 | 0 | — | — |
| `Madouqu` | True | <REDACTED len=19> | — | — | 0 | 0 | — | — |
| `Mgstage` | True | <REDACTED len=23> | — | — | 0 | 0 | — | — |
| `Mmtv` | True | <REDACTED len=19> | — | — | 0 | 0 | — | — |
| `ThePornDB` | True | <REDACTED len=25> | — | — | 0 | 0 | — | — |
| `airav_io` | True | <REDACTED len=19> | — | — | 0 | 0 | — | — |
| `avbase` | False | <REDACTED len=22> | — | — | 0 | 0 | — | — |
| `avmoo` | False | <REDACTED len=21> | — | — | 0 | 0 | — | — |
| `avsox` | False | <REDACTED len=19> | — | — | 0 | 0 | — | — |
| `dmm` | True | <REDACTED len=21> | — | — | 0 | 0 | — | — |
| `fc2_hub` | True | <REDACTED len=18> | — | — | 0 | 0 | — | — |
| `freejavbt` | True | <REDACTED len=21> | — | — | 0 | 0 | — | — |
| `hbox_jp` | True | <REDACTED len=15> | — | — | 0 | 0 | — | — |
| `jav321` | True | <REDACTED len=22> | — | — | 0 | 0 | — | — |
| `miss_av` | True | <REDACTED len=21> | — | — | 0 | 0 | — | — |
| `xiao_huang_shu` | True | <REDACTED len=17> | — | — | 0 | 0 | — | — |

## 额外字段

- `api_key`: 出现在 21/21 个源
- `base_url`: 出现在 21/21 个源
- `cookies`: 出现在 21/21 个源
- `cooldown_seconds`: 出现在 21/21 个源
- `disable_hash_match`: 出现在 21/21 个源
- `enabled`: 出现在 21/21 个源
- `proxy`: 出现在 21/21 个源
- `retry_times`: 出现在 21/21 个源
- `retry_times_override`: 出现在 21/21 个源
- `user_agent`: 出现在 21/21 个源

## 全局优先级 `priorities`

- **jav_censored**: dmm → Mgstage → Javlibrary → avbase → hbox_jp → Javdb → Javbus → jav321 → avmoo → Mmtv → airav_io → freejavbt → miss_av
- **jav_uncensored**: Carib → avbase → Javbus → Javdb → avsox → Mmtv → airav_io → freejavbt → miss_av
- **jav_amateur**: Mgstage → Carib → Javlibrary → avsox → avmoo → Javbus → Javdb → jav321 → Mmtv → airav_io → freejavbt → miss_av
- **jav_fc2**: Fc2 → fc2_hub → Javdb → avsox → Mmtv → airav_io → freejavbt → miss_av
- **cn**: Madouqu → Madou → xiao_huang_shu → Mmtv
- **ea**: ThePornDB
- **by_fields**: Outline → ExtraFanart → OriginalTitle → UserRating → Tags → Title → Cover → Poster
- **ignore_fields**: 

## 字段优先级

（顶层未出现，字段级优先级在别处）

## 其他

- `default_retry_times`: 0
- `priorities_user_modified`: False
- `series_type_keywords`: {"jav_censored": [], "jav_uncensored": [], "jav_amateur": [], "cn": [], "ea": []}
- `series_type_keywords_in_path`: {"jav_censored": [], "jav_uncensored": [], "jav_amateur": [], "cn": [], "ea": []}
