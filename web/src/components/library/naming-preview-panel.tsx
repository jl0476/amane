import { Alert, Box, Group, Loader, Paper, Select, Stack, Text, TextInput } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { previewNaming } from "@/client/sdk.gen";
import type { NamingPreviewRequest, NamingPreviewResponse } from "@/client/types.gen";
import { EnumToggle } from "@/components/common/enum-toggle";
import { extractErrorMessage } from "@/lib/api-error";
import { isOneOf } from "@/lib/exhaustive";
import { CONTENT_TYPES, NAMING_SYNTAXES, type NamingSyntax } from "@/lib/exhaustive-maps";

const PREVIEW_FIELDS = [
  "video",
  "thumb",
  "poster",
  "fanart",
  "nfo",
  "trailer",
  "subtitle",
] as const;

/** 试算输入的防抖间隔: 每次击键都请求会把中间态刷成一片. */
const PREVIEW_DEBOUNCE_MS = 400;

interface SampleInputState {
  number: string;
  source_path: string;
  content_type: NamingPreviewRequest["content_type"];
  title: string;
  studio: string;
  actors: string;
  release: string;
}

function emptySample(): SampleInputState {
  return {
    number: "",
    source_path: "",
    content_type: "censored",
    title: "",
    studio: "",
    actors: "",
    release: "",
  };
}

/**
 * 模板编辑器旁的「试算」面板 (规格 3.2.4): POST /api/libraries/naming/preview,
 * 实时显示解析结果与各产物渲染路径; 渲染或校验失败显示 422 错误文本.
 *
 * 端点不含模板字段: 渲染按库 (或全局默认) 已保存的生效配置, syntax 只切换语法.
 * 未保存的表单修改不参与试算, 因此面板提示文案明确这一点.
 */
export function NamingPreviewPanel({
  libraryId,
  syntax,
  onSyntaxChange,
}: {
  libraryId: number | null;
  syntax: NamingSyntax;
  onSyntaxChange: (syntax: NamingSyntax) => void;
}) {
  const { t } = useTranslation("library");
  const [sample, setSample] = useState<SampleInputState>(emptySample);
  const [debounced, setDebounced] = useState<SampleInputState>(sample);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(sample), PREVIEW_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [sample]);

  const requestBody: NamingPreviewRequest = {
    library_id: libraryId ?? undefined,
    syntax,
    number: debounced.number.trim() === "" ? undefined : debounced.number.trim(),
    source_path: debounced.source_path.trim() === "" ? undefined : debounced.source_path.trim(),
    content_type: debounced.content_type,
    title: debounced.title.trim() === "" ? undefined : debounced.title.trim(),
    studio: debounced.studio.trim() === "" ? undefined : debounced.studio.trim(),
    actors: debounced.actors
      .split(",")
      .map((actor) => actor.trim())
      .filter((actor) => actor !== ""),
    release: debounced.release.trim() === "" ? undefined : debounced.release.trim(),
  };

  const previewQuery = useQuery({
    queryKey: ["naming-preview", requestBody],
    queryFn: async () => {
      const { data } = await previewNaming({ body: requestBody, throwOnError: true });
      return data;
    },
    retry: false,
    staleTime: 30_000,
  });

  return (
    <Paper withBorder p="md" radius="sm">
      <Stack gap="sm">
        <Group justify="space-between" wrap="nowrap">
          <Text fw={600} size="sm">
            {t("preview.title")}
          </Text>
          <Group gap={8} wrap="nowrap" align="center">
            <Text size="sm" c="dimmed">
              {t("preview.syntax")}
            </Text>
            <EnumToggle
              options={NAMING_SYNTAXES}
              value={syntax}
              onChange={onSyntaxChange}
              getLabel={(value) => t(`syntax.${value}`)}
            />
          </Group>
        </Group>
        <Text size="xs" c="dimmed">
          {libraryId == null ? t("preview.hintGlobal") : t("preview.hint")}
        </Text>
        <Group grow preventGrowOverflow={false} wrap="wrap" gap="xs">
          <TextInput
            size="xs"
            label={t("preview.number")}
            value={sample.number}
            onChange={(event) => setSample({ ...sample, number: event.currentTarget.value })}
            style={{ flex: "1 1 10rem" }}
          />
          <TextInput
            size="xs"
            label={t("preview.sampleTitle")}
            value={sample.title}
            onChange={(event) => setSample({ ...sample, title: event.currentTarget.value })}
            style={{ flex: "2 1 16rem" }}
          />
          <TextInput
            size="xs"
            label={t("preview.studio")}
            value={sample.studio}
            onChange={(event) => setSample({ ...sample, studio: event.currentTarget.value })}
            style={{ flex: "1 1 10rem" }}
          />
          <TextInput
            size="xs"
            label={t("preview.actors")}
            placeholder={t("preview.actorsPlaceholder")}
            value={sample.actors}
            onChange={(event) => setSample({ ...sample, actors: event.currentTarget.value })}
            style={{ flex: "1 1 12rem" }}
          />
          <Select
            size="xs"
            label={t("preview.contentType")}
            allowDeselect={false}
            data={CONTENT_TYPES.map((kind) => ({
              value: kind,
              label: t(`preview.contentTypes.${kind}`),
            }))}
            value={sample.content_type}
            onChange={(raw) => {
              if (isOneOf(CONTENT_TYPES, raw)) {
                setSample({ ...sample, content_type: raw });
              }
            }}
            style={{ flex: "1 1 8rem" }}
          />
          <TextInput
            size="xs"
            label={t("preview.release")}
            value={sample.release}
            onChange={(event) => setSample({ ...sample, release: event.currentTarget.value })}
            style={{ flex: "1 1 10rem" }}
          />
          <TextInput
            size="xs"
            label={t("preview.sourcePath")}
            value={sample.source_path}
            onChange={(event) => setSample({ ...sample, source_path: event.currentTarget.value })}
            style={{ flex: "1 1 16rem" }}
          />
        </Group>
        {previewQuery.isError && (
          <Alert color="red" variant="light" title={t("preview.failed")}>
            {extractErrorMessage(previewQuery.error, t("preview.failed"))}
          </Alert>
        )}
        {previewQuery.isPending ? (
          <Box py="md" style={{ display: "flex", justifyContent: "center" }}>
            <Loader size="sm" />
          </Box>
        ) : (
          previewQuery.data != null && <PreviewResult result={previewQuery.data} />
        )}
      </Stack>
    </Paper>
  );
}

function PreviewResult({ result }: { result: NamingPreviewResponse }) {
  const { t } = useTranslation("library");
  return (
    <Stack gap="xs">
      <Group gap="xs" wrap="wrap">
        <Text size="xs" c="dimmed" ff="monospace">
          {result.number}
        </Text>
        <Text size="xs" c="dimmed">
          {result.content_type} · {result.mosaic}
          {result.subtitle_tag !== ""
            ? ` · ${t("preview.subtitleTag", { tag: result.subtitle_tag })}`
            : ""}
          {` · ${result.definition}`}
        </Text>
      </Group>
      <Stack gap={2}>
        {PREVIEW_FIELDS.map((field) => (
          <Group key={field} gap="sm" wrap="nowrap" align="baseline">
            <Text size="xs" c="dimmed" w={72} visibleFrom="sm">
              {t(`preview.fields.${field}`)}
            </Text>
            <Text size="xs" ff="monospace" style={{ wordBreak: "break-all" }}>
              {result[field]}
            </Text>
          </Group>
        ))}
      </Stack>
    </Stack>
  );
}
