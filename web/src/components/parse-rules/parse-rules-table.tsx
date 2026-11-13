import { ActionIcon, Badge, Box, Group, Menu, Skeleton, Switch, Table, Text } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconDots, IconPencil, IconTrash } from "@tabler/icons-react";
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { listParseRulesQueryKey } from "@/client/@tanstack/react-query.gen";
import { deleteParseRule, updateParseRule } from "@/client/sdk.gen";
import type { ParseRuleResponse, ParseRuleUpsertRequest } from "@/client/types.gen";
import { HintedActionIcon } from "@/components/common/hinted-action-icon";
import { ListToolbar } from "@/components/common/list-toolbar";
import { extractErrorMessage } from "@/lib/api-error";
import { confirm } from "@/lib/confirm";

const CELL_OVERFLOW = { overflow: "hidden", maxWidth: 0 } as const;

export interface ParseRulesTableProps {
  items: ParseRuleResponse[];
  isLoading: boolean;
  totalPages: number;
  page: number;
  onPageChange: (page: number) => void;
  onEdit: (rule: ParseRuleResponse) => void;
  /** scope_id → 库名; 全局行不查. */
  libraryNames: Map<number, string>;
}

/**
 * 识别词表 (规格 3.1.7): 多行记录, 作用域 (全局 / 库级) 是行属性.
 * 行内启用开关走整体替换的 PUT, 因此提交完整 upsert 体.
 */
export function ParseRulesTable({
  items,
  isLoading,
  totalPages,
  page,
  onPageChange,
  onEdit,
  libraryNames,
}: ParseRulesTableProps) {
  const { t } = useTranslation(["parseRules", "common"]);
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState(false);

  function invalidate() {
    void queryClient.invalidateQueries({ queryKey: listParseRulesQueryKey() });
  }

  async function handleToggleEnabled(rule: ParseRuleResponse) {
    setBusy(true);
    try {
      await updateParseRule({
        path: { rule_id: rule.id },
        body: upsertBody(rule, { enabled: !rule.enabled }),
        throwOnError: true,
      });
      invalidate();
    } catch (err) {
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      });
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete(rule: ParseRuleResponse) {
    const ok = await confirm({
      title: t("confirmDelete.title"),
      message: t("confirmDelete.message", { pattern: rule.pattern }),
      confirmLabel: t("common:actions.delete"),
    });
    if (!ok) {
      return;
    }
    setBusy(true);
    try {
      await deleteParseRule({ path: { rule_id: rule.id }, throwOnError: true });
      invalidate();
    } catch (err) {
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <ListToolbar totalPages={totalPages} page={page} onChange={onPageChange}>
      <Table stickyHeader highlightOnHover verticalSpacing="sm" layout="fixed" w="100%">
        <Table.Thead>
          <Table.Tr>
            <Table.Th w={120}>{t("columns.scope")}</Table.Th>
            <Table.Th w={130}>{t("columns.kind")}</Table.Th>
            <Table.Th w={110}>{t("columns.contentType")}</Table.Th>
            <Table.Th>{t("columns.pattern")}</Table.Th>
            <Table.Th w={80}>{t("columns.enabled")}</Table.Th>
            <Table.Th w={200} visibleFrom="md">
              {t("columns.updated")}
            </Table.Th>
            <Table.Th w={90} ta="right">
              {t("columns.actions")}
            </Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {items.map((rule) => (
            <Table.Tr key={rule.id}>
              <Table.Td style={CELL_OVERFLOW}>
                {rule.scope_type === "global" ? (
                  <Badge size="sm" variant="light" color="gray">
                    {t("scope.global")}
                  </Badge>
                ) : (
                  <Badge size="sm" variant="light" title={String(rule.scope_id)}>
                    {libraryNames.get(rule.scope_id) ?? `#${rule.scope_id}`}
                  </Badge>
                )}
              </Table.Td>
              <Table.Td style={CELL_OVERFLOW}>
                <Text size="sm">{t(`kind.${rule.kind}`)}</Text>
              </Table.Td>
              <Table.Td style={CELL_OVERFLOW}>
                <Badge size="sm" variant="outline">
                  {t(`contentTypes.${rule.content_type}`)}
                </Badge>
              </Table.Td>
              <Table.Td style={CELL_OVERFLOW}>
                <Text size="xs" ff="monospace" truncate title={rule.pattern}>
                  {rule.pattern}
                </Text>
              </Table.Td>
              <Table.Td>
                <Switch
                  checked={rule.enabled}
                  disabled={busy}
                  onChange={() => void handleToggleEnabled(rule)}
                  aria-label={t("columns.enabled")}
                />
              </Table.Td>
              <Table.Td style={CELL_OVERFLOW} visibleFrom="md">
                <Text size="sm" c="dimmed">
                  {new Date(rule.updated_at).toLocaleString()}
                </Text>
              </Table.Td>
              <Table.Td>
                <Group gap={4} justify="flex-end" wrap="nowrap" visibleFrom="sm">
                  <HintedActionIcon
                    variant="subtle"
                    disabled={busy}
                    label={t("common:actions.edit")}
                    onClick={() => onEdit(rule)}
                  >
                    <IconPencil size={16} />
                  </HintedActionIcon>
                  <HintedActionIcon
                    variant="subtle"
                    color="red"
                    disabled={busy}
                    label={t("common:actions.delete")}
                    onClick={() => void handleDelete(rule)}
                  >
                    <IconTrash size={16} />
                  </HintedActionIcon>
                </Group>
                <Box hiddenFrom="sm" style={{ display: "flex", justifyContent: "flex-end" }}>
                  <Menu position="bottom-end" withinPortal>
                    <Menu.Target>
                      <ActionIcon size="sm" variant="subtle" aria-label={t("columns.actions")}>
                        <IconDots size={14} />
                      </ActionIcon>
                    </Menu.Target>
                    <Menu.Dropdown>
                      <Menu.Item
                        leftSection={<IconPencil size={14} />}
                        onClick={() => onEdit(rule)}
                      >
                        {t("common:actions.edit")}
                      </Menu.Item>
                      <Menu.Divider />
                      <Menu.Item
                        color="red"
                        leftSection={<IconTrash size={14} />}
                        disabled={busy}
                        onClick={() => void handleDelete(rule)}
                      >
                        {t("common:actions.delete")}
                      </Menu.Item>
                    </Menu.Dropdown>
                  </Menu>
                </Box>
              </Table.Td>
            </Table.Tr>
          ))}
          {isLoading &&
            items.length === 0 &&
            Array.from({ length: 5 }, (_, i) => (
              <Table.Tr key={`sk-${i}`}>
                <Table.Td>
                  <Skeleton h={14} w={60} />
                </Table.Td>
                <Table.Td>
                  <Skeleton h={14} w={80} />
                </Table.Td>
                <Table.Td>
                  <Skeleton h={14} w={50} />
                </Table.Td>
                <Table.Td>
                  <Skeleton h={14} w="80%" />
                </Table.Td>
                <Table.Td>
                  <Skeleton h={18} w={36} />
                </Table.Td>
                <Table.Td visibleFrom="md">
                  <Skeleton h={14} w={120} />
                </Table.Td>
                <Table.Td>
                  <Skeleton h={14} w={60} ml="auto" />
                </Table.Td>
              </Table.Tr>
            ))}
        </Table.Tbody>
      </Table>
    </ListToolbar>
  );
}

/** PUT / DELETE 共用的整体替换体: 以既有行补全全部必填键, 只允许 patch 指定的键变化. */
export function upsertBody(
  rule: ParseRuleResponse,
  patch: Partial<
    Pick<ParseRuleUpsertRequest, "enabled" | "pattern" | "kind" | "content_type">
  > = {},
): ParseRuleUpsertRequest {
  return {
    scope_type: rule.scope_type === "global" ? "global" : "library",
    scope_id: rule.scope_id,
    kind: patch.kind ?? rule.kind,
    content_type: patch.content_type ?? rule.content_type,
    pattern: patch.pattern ?? rule.pattern,
    enabled: patch.enabled ?? rule.enabled,
  };
}
