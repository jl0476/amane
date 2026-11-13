import { Alert, Badge, Group, Loader, Paper, Stack, Switch, Text, Title } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconAlertCircle } from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  deleteLibraryOverrideMutation,
  getEffectiveConfigOptions,
  getEffectiveConfigQueryKey,
  listLibraryOverridesOptions,
  listLibraryOverridesQueryKey,
  putLibraryOverrideMutation,
} from "@/client/@tanstack/react-query.gen";
import { resolveSchema, type JSONSchemaObject } from "@/components/schema-form/schema";
import { SchemaForm } from "@/components/schema-form/schema-form";
import { useConfig, useConfigSchema } from "@/hooks/use-config";
import { extractErrorMessage } from "@/lib/api-error";
import { confirm } from "@/lib/confirm";
import {
  deepEqual,
  isPlainRecord,
  OVERRIDE_SECTIONS,
  type OverrideSection,
} from "@/lib/library/overrides";

/**
 * 库详情页「配置覆盖」Tab (规格 3.1.7): 按 section 分块编辑稀疏覆盖.
 *
 * 每个 section 一处「继承全局」开关: 勾选表示继承全局 (无覆盖行), 取消勾选后经
 * SchemaForm patch 模式编辑, 保存时合成为稀疏 payload 整体替换 (PUT); 重新勾选删除覆盖行.
 * 可空模板字段清空时表单编码为 null, 在此统一翻译为「显式取空」哨兵 {} —— 覆盖 payload 拒绝 null.
 */
export function OverridesEditor({ libraryId }: { libraryId: number }) {
  const { t } = useTranslation(["library", "common"]);
  const queryClient = useQueryClient();

  const overridesQuery = useQuery(listLibraryOverridesOptions({ path: { library_id: libraryId } }));
  const effectiveQuery = useQuery(getEffectiveConfigOptions({ path: { library_id: libraryId } }));
  const schemaQuery = useConfigSchema();
  const globalValuesQuery = useConfig();

  const putMutation = useMutation({
    ...putLibraryOverrideMutation(),
    onSuccess: () => {
      notifications.show({ message: t("overrides.saved"), color: "green" });
      invalidate();
    },
    onError: (err) =>
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      }),
  });
  const deleteMutation = useMutation({
    ...deleteLibraryOverrideMutation(),
    onSuccess: () => {
      invalidate();
    },
    onError: (err) =>
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      }),
  });

  function invalidate() {
    void queryClient.invalidateQueries({
      queryKey: listLibraryOverridesQueryKey({ path: { library_id: libraryId } }),
    });
    void queryClient.invalidateQueries({
      queryKey: getEffectiveConfigQueryKey({ path: { library_id: libraryId } }),
    });
  }

  const resolvedSchema = useMemo(
    () => (schemaQuery.data ? resolveSchema(schemaQuery.data, schemaQuery.data) : null),
    [schemaQuery.data],
  );

  const loading =
    overridesQuery.isLoading ||
    effectiveQuery.isLoading ||
    schemaQuery.isLoading ||
    globalValuesQuery.isLoading;
  const error =
    overridesQuery.error ?? effectiveQuery.error ?? schemaQuery.error ?? globalValuesQuery.error;

  if (loading) {
    return <Loader size="sm" />;
  }
  if (error != null || resolvedSchema == null) {
    return (
      <Alert color="red" icon={<IconAlertCircle size={18} />} variant="light">
        {extractErrorMessage(error, t("overrides.loadFailed"))}
      </Alert>
    );
  }

  const rows = overridesQuery.data?.overrides ?? {};

  return (
    <Stack gap="md">
      <Text size="sm" c="dimmed">
        {t("overrides.hint")}
      </Text>
      {OVERRIDE_SECTIONS.map((section) => (
        <SectionBlock
          key={section}
          section={section}
          rowPayload={rows[section]}
          sectionSchema={resolvedSchema.properties?.[section]}
          globalValues={globalValuesQuery.data?.[section]}
          effective={effectiveQuery.data?.[section]}
          saving={putMutation.isPending || deleteMutation.isPending}
          onSave={(payload) =>
            putMutation.mutate({ path: { library_id: libraryId, section }, body: payload })
          }
          onInherit={() => deleteMutation.mutate({ path: { library_id: libraryId, section } })}
        />
      ))}
    </Stack>
  );
}

interface SectionBlockProps {
  section: OverrideSection;
  /** 该 section 现有覆盖行 payload; 无覆盖行时 undefined. */
  rowPayload: Record<string, unknown> | undefined;
  /** HotSettings schema 中该 section 的 (已解引用) 子 schema. */
  sectionSchema: JSONSchemaObject | undefined;
  /** 全局配置值 (合成基准: 与全局同值的键不落覆盖行). */
  globalValues: Record<string, unknown> | undefined;
  /** 三节合成后的生效值 (编辑器初值的无覆盖键来源). */
  effective: unknown;
  saving: boolean;
  onSave: (payload: Record<string, unknown>) => void;
  onInherit: () => void;
}

function SectionBlock({
  section,
  rowPayload,
  sectionSchema,
  globalValues,
  effective,
  saving,
  onSave,
  onInherit,
}: SectionBlockProps) {
  const { t } = useTranslation(["library", "settings", "common"]);
  const inheriting = rowPayload == null;
  // 覆盖行不存在时无法从 props 区分「继承」与「编辑」: 取消勾选进入本地草稿态渲染
  // SchemaForm (初值取生效值), PUT 落库后 rowPayload 出现, inheriting 随之转 false.
  const [drafting, setDrafting] = useState(false);
  const editing = !inheriting || drafting;

  const initialValues = useMemo(() => {
    const values: Record<string, unknown> = {};
    const effectiveRecord = isPlainRecord(effective) ? effective : {};
    const row = rowPayload ?? {};
    const properties = isPlainRecord(sectionSchema) ? sectionSchema.properties : undefined;
    for (const key of Object.keys(properties ?? {})) {
      if (key in row) {
        // 「显式取空」哨兵 {} → null, 与可空模板字段的表单空值同形.
        values[key] = deepEqual(row[key], {}) ? null : row[key];
      } else {
        values[key] = effectiveRecord[key] ?? null;
      }
    }
    return values;
  }, [effective, rowPayload, sectionSchema]);

  function handleSave(patch: Record<string, unknown>) {
    const merged: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(rowPayload ?? {})) {
      merged[key] = value;
    }
    for (const [key, value] of Object.entries(patch)) {
      // 表单把可空模板字段清空编码为 null; 覆盖 payload 以 {} 表达显式取空.
      merged[key] = value === null ? {} : value;
    }
    // 与全局同值的键不落覆盖行: 继承即得同值, 留在行里只会在全局变化后钉死旧值.
    for (const key of Object.keys(merged)) {
      if (deepEqual(merged[key], globalValues?.[key])) {
        delete merged[key];
      }
    }
    if (Object.keys(merged).length > 0) {
      onSave(merged);
    } else if (rowPayload != null) {
      // 全部键都回到继承: 空覆盖行无意义, 删行恢复继承.
      onInherit();
    } else {
      // 草稿全部字段与全局同值: 无覆盖可落, 退回继承显示.
      setDrafting(false);
    }
  }

  async function handleInheritToggle(checked: boolean) {
    if (checked) {
      const ok = await confirm({
        title: t("overrides.inheritConfirmTitle"),
        message: t("overrides.inheritConfirmBody"),
        confirmLabel: t("common:actions.confirm"),
      });
      if (!ok) {
        return;
      }
      if (rowPayload != null) {
        onInherit();
      }
      setDrafting(false);
    } else {
      setDrafting(true);
    }
  }

  return (
    <Paper withBorder p="md">
      <Group justify="space-between" wrap="nowrap" mb={editing ? "md" : 0}>
        <Group gap="sm" wrap="nowrap">
          <Title order={4}>{t(`settings:tabs.${section}`)}</Title>
          {!inheriting && (
            <Badge size="sm" variant="light" color="blue">
              {t("overrides.overriding")}
            </Badge>
          )}
        </Group>
        <Switch
          label={t("overrides.inheritGlobal")}
          description={editing ? t("overrides.inheritOffHint") : undefined}
          checked={!editing}
          disabled={saving}
          onChange={(event) => void handleInheritToggle(event.currentTarget.checked)}
        />
      </Group>
      {!editing ? (
        <Text size="sm" c="dimmed">
          {t("overrides.inheritHint")}
        </Text>
      ) : (
        sectionSchema != null &&
        typeof sectionSchema !== "boolean" && (
          <SchemaForm
            schema={sectionSchema}
            prefix={section}
            values={initialValues}
            i18nPrefix="settings:fields"
            fieldLayout="grid"
            onSave={handleSave}
            saving={saving}
          />
        )
      )}
    </Paper>
  );
}
