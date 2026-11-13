/**
 * 库配置覆盖 (规格 3.1) 的前端契约: section 白名单、稀疏 payload 合成与值比较.
 *
 * section 集合与后端 `config/overrides.py::SECTION_MODELS` 一致; 后端扩节时
 * `EffectiveConfigResponse` 会先多出成员, 此处 `exhaustiveTuple` 编译失败迫使同步.
 */

import type { EffectiveConfigResponse } from "@/client/types.gen";
import { exhaustiveTuple } from "@/lib/exhaustive";

export type OverrideSection = keyof EffectiveConfigResponse & string;

export const OVERRIDE_SECTIONS = exhaustiveTuple<OverrideSection>()("common", "naming", "nfo");

/** Deep equality for config values (primitives, arrays, plain objects). */
export function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (a == null || b == null) return a === b;
  if (typeof a !== typeof b) return false;
  if (Array.isArray(a)) {
    if (!Array.isArray(b) || a.length !== b.length) return false;
    return a.every((item, i) => deepEqual(item, b[i]));
  }
  if (isPlainRecord(a) && isPlainRecord(b)) {
    const keysA = Object.keys(a);
    const keysB = Object.keys(b);
    if (keysA.length !== keysB.length) return false;
    return keysA.every((key) => deepEqual(a[key], b[key]));
  }
  return false;
}

export function isPlainRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
