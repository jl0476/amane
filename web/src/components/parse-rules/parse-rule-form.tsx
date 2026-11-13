import { Group, Select, Switch, TextInput } from "@mantine/core";
import { useTranslation } from "react-i18next";
import type { ContentType, ParseRuleUpsertRequest, RuleKind } from "@/client/types.gen";
import { CONTENT_TYPES } from "@/lib/exhaustive-maps";

/** 识别词在表单内的可编辑形态; 每项都是必填, 唯一的可选键是 enabled. */
export interface ParseRuleFormState {
  scope_type: ParseRuleUpsertRequest["scope_type"];
  scope_id: number | null;
  kind: RuleKind;
  content_type: ContentType;
  pattern: string;
  enabled: boolean;
}

/** 后端模式长度上限; RE2 合法性由保存路径校验 (422 带具体原因). */
const PATTERN_MAX_LENGTH = 256;

export function emptyRuleForm(): ParseRuleFormState {
  return {
    scope_type: "global",
    scope_id: null,
    kind: "number_keyword",
    content_type: "censored",
    pattern: "",
    enabled: true,
  };
}

export function ruleFormFromResponse(rule: ParseRuleFormSource): ParseRuleFormState {
  return {
    scope_type: rule.scope_type === "global" ? "global" : "library",
    scope_id: rule.scope_type === "global" ? null : rule.scope_id,
    kind: rule.kind,
    content_type: rule.content_type,
    pattern: rule.pattern,
    enabled: rule.enabled,
  };
}

/** 编辑来源: API 响应行或草稿, 两者都带表单需要的键. */
export interface ParseRuleFormSource {
  scope_type: string;
  scope_id: number;
  kind: RuleKind;
  content_type: ContentType;
  pattern: string;
  enabled: boolean;
}

export function ruleFormToBody(form: ParseRuleFormState): ParseRuleUpsertRequest {
  return {
    scope_type: form.scope_type,
    // scope_type=global 时后端恒按 0 落库; 库级必须带正数 id.
    scope_id: form.scope_type === "global" ? undefined : (form.scope_id ?? undefined),
    kind: form.kind,
    content_type: form.content_type,
    pattern: form.pattern.trim(),
    enabled: form.enabled,
  };
}

export function ruleFormCanSubmit(form: ParseRuleFormState): boolean {
  if (form.pattern.trim() === "" || form.pattern.length > PATTERN_MAX_LENGTH) {
    return false;
  }
  if (form.scope_type === "library" && (form.scope_id == null || form.scope_id <= 0)) {
    return false;
  }
  return true;
}

interface ParseRuleFormFieldsProps {
  value: ParseRuleFormState;
  onChange: (value: ParseRuleFormState) => void;
  /** 库级作用域的候选项: id → 名称. */
  libraryOptions: Array<{ value: string; label: string }>;
}

export function ParseRuleFormFields({ value, onChange, libraryOptions }: ParseRuleFormFieldsProps) {
  const { t } = useTranslation("parseRules");
  const kindOptions: Array<{ value: RuleKind; label: string }> = [
    { value: "number_keyword", label: t("kind.number_keyword") },
    { value: "path_keyword", label: t("kind.path_keyword") },
  ];
  const scopeOptions: Array<{ value: ParseRuleUpsertRequest["scope_type"]; label: string }> = [
    { value: "global", label: t("scope.global") },
    { value: "library", label: t("scope.library") },
  ];
  const contentTypeOptions = CONTENT_TYPES.map((kind) => ({
    value: kind,
    label: t(`contentTypes.${kind}`),
  }));

  return (
    <Group grow preventGrowOverflow={false} wrap="wrap">
      <Select
        label={t("fields.scopeType")}
        allowDeselect={false}
        data={scopeOptions}
        value={value.scope_type}
        onChange={(raw) => {
          if (raw === "global" || raw === "library") {
            onChange({
              ...value,
              scope_type: raw,
              scope_id: raw === "global" ? null : value.scope_id,
            });
          }
        }}
        style={{ flex: "1 1 10rem" }}
      />
      {value.scope_type === "library" && (
        <Select
          label={t("fields.scopeLibrary")}
          allowDeselect={false}
          data={libraryOptions}
          value={value.scope_id != null ? String(value.scope_id) : ""}
          onChange={(raw) => onChange({ ...value, scope_id: raw != null ? Number(raw) : null })}
          style={{ flex: "1 1 12rem" }}
        />
      )}
      <Select
        label={t("fields.kind")}
        allowDeselect={false}
        data={kindOptions}
        value={value.kind}
        onChange={(raw) => {
          if (raw === "number_keyword" || raw === "path_keyword") {
            onChange({ ...value, kind: raw });
          }
        }}
        style={{ flex: "1 1 10rem" }}
      />
      <Select
        label={t("fields.contentType")}
        allowDeselect={false}
        data={contentTypeOptions}
        value={value.content_type}
        onChange={(raw) => {
          if (raw != null && CONTENT_TYPES.some((kind) => kind === raw)) {
            onChange({ ...value, content_type: raw });
          }
        }}
        style={{ flex: "1 1 10rem" }}
      />
      <TextInput
        label={t("fields.pattern")}
        description={t("fields.patternHint")}
        error={
          value.pattern.length > PATTERN_MAX_LENGTH
            ? t("fields.patternTooLong", { max: PATTERN_MAX_LENGTH })
            : undefined
        }
        value={value.pattern}
        onChange={(event) => onChange({ ...value, pattern: event.currentTarget.value })}
        style={{ flex: "1 1 100%" }}
      />
      <Switch
        label={t("fields.enabled")}
        checked={value.enabled}
        onChange={(event) => onChange({ ...value, enabled: event.currentTarget.checked })}
      />
    </Group>
  );
}
