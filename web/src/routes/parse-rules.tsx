import {
  Button,
  Group,
  Modal,
  SegmentedControl,
  Stack,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconPlus, IconSearch } from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createFileRoute, stripSearchParams } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { z } from "zod";
import {
  createParseRuleMutation,
  listLibrariesOptions,
  listParseRulesOptions,
  listParseRulesQueryKey,
  updateParseRuleMutation,
} from "@/client/@tanstack/react-query.gen";
import type { ParseRuleResponse } from "@/client/types.gen";
import { BrowsePageShell } from "@/components/common/browse-page-shell";
import {
  emptyRuleForm,
  ParseRuleFormFields,
  ruleFormCanSubmit,
  ruleFormFromResponse,
  ruleFormToBody,
  type ParseRuleFormState,
} from "@/components/parse-rules/parse-rule-form";
import { ParseRulesTable } from "@/components/parse-rules/parse-rules-table";
import { extractErrorMessage } from "@/lib/api-error";

const PAGE_SIZE = 50;

const RULE_SCOPES = ["all", "global", "library"] as const;
type RuleScope = (typeof RULE_SCOPES)[number];

const parseRulesSearchSchema = z.object({
  q: z.string().optional(),
  scope: z.enum(RULE_SCOPES).optional(),
  page: z.coerce.number().int().min(1).catch(1).default(1),
});

export const Route = createFileRoute("/parse-rules")({
  validateSearch: parseRulesSearchSchema,
  search: { middlewares: [stripSearchParams({ page: 1, scope: "all" })] },
  component: ParseRulesPage,
});

/** 表格展示次序: 全局在前, 库级按库 id 与类型聚合, 段内按创建时间升序 (与匹配次序同向). */
function sortRules(rules: ParseRuleResponse[]): ParseRuleResponse[] {
  return rules.toSorted((a, b) => {
    if (a.scope_type !== b.scope_type) {
      return a.scope_type === "global" ? -1 : 1;
    }
    if (a.scope_id !== b.scope_id) {
      return a.scope_id - b.scope_id;
    }
    if (a.kind !== b.kind) {
      return a.kind < b.kind ? -1 : 1;
    }
    if (a.created_at !== b.created_at) {
      return a.created_at < b.created_at ? -1 : 1;
    }
    return a.id - b.id;
  });
}

function ParseRulesPage() {
  const { t } = useTranslation(["parseRules", "common"]);
  const queryClient = useQueryClient();
  const search = Route.useSearch();
  const navigate = Route.useNavigate();

  const rulesQuery = useQuery(listParseRulesOptions());
  const librariesQuery = useQuery(listLibrariesOptions());

  const [createOpen, setCreateOpen] = useState(false);
  const [createForm, setCreateForm] = useState<ParseRuleFormState>(emptyRuleForm);
  const [editing, setEditing] = useState<ParseRuleResponse | null>(null);
  const [editForm, setEditForm] = useState<ParseRuleFormState>(emptyRuleForm);

  const libraryNames = useMemo(() => {
    const names = new Map<number, string>();
    for (const lib of librariesQuery.data?.items ?? []) {
      names.set(lib.id, lib.name);
    }
    return names;
  }, [librariesQuery.data]);

  const filtered = useMemo(() => {
    const scope = search.scope ?? "all";
    const q = (search.q ?? "").trim().toLowerCase();
    return sortRules(rulesQuery.data?.items ?? []).filter((rule) => {
      if (scope !== "all" && rule.scope_type !== scope) {
        return false;
      }
      if (q !== "" && !rule.pattern.toLowerCase().includes(q)) {
        return false;
      }
      return true;
    });
  }, [rulesQuery.data, search.q, search.scope]);

  const total = filtered.length;
  const offset = (search.page - 1) * PAGE_SIZE;
  const pageItems = filtered.slice(offset, offset + PAGE_SIZE);

  function patchSearch(patch: Partial<{ q: string | undefined; scope: RuleScope; page: number }>) {
    void navigate({ search: (prev) => ({ ...prev, ...patch }) });
  }

  function invalidate() {
    void queryClient.invalidateQueries({ queryKey: listParseRulesQueryKey() });
  }

  const createMutation = useMutation({
    ...createParseRuleMutation(),
    onSuccess: () => {
      notifications.show({ message: t("toast.created"), color: "green" });
      setCreateOpen(false);
      setCreateForm(emptyRuleForm());
      invalidate();
    },
    onError: (err) =>
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      }),
  });
  const updateMutation = useMutation({
    ...updateParseRuleMutation(),
    onSuccess: () => {
      notifications.show({ message: t("toast.updated"), color: "green" });
      setEditing(null);
      invalidate();
    },
    onError: (err) =>
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      }),
  });

  const libraryOptions = useMemo(
    () =>
      (librariesQuery.data?.items ?? []).map((lib) => ({
        value: String(lib.id),
        label: lib.name,
      })),
    [librariesQuery.data],
  );

  return (
    <>
      <BrowsePageShell
        fill
        title={
          <Stack gap={4}>
            <Title order={2}>{t("title")}</Title>
            <Text size="xs" c="dimmed">
              {t("hint")}
            </Text>
          </Stack>
        }
        summary={
          <Text size="sm" c="dimmed">
            {t("common:pagination.totalItems", { count: total })}
          </Text>
        }
        search={
          <TextInput
            value={search.q ?? ""}
            onChange={(event) =>
              patchSearch({ q: event.currentTarget.value || undefined, page: 1 })
            }
            placeholder={t("searchPlaceholder")}
            leftSection={<IconSearch size={16} />}
            w="100%"
          />
        }
        extras={
          <SegmentedControl
            value={search.scope ?? "all"}
            onChange={(value) => {
              if (RULE_SCOPES.some((scope) => scope === value)) {
                patchSearch({ scope: value, page: 1 });
              }
            }}
            data={RULE_SCOPES.map((scope) => ({ value: scope, label: t(`scope.${scope}`) }))}
          />
        }
        actions={
          <Button
            leftSection={<IconPlus size={16} />}
            onClick={() => {
              setCreateForm(emptyRuleForm());
              setCreateOpen(true);
            }}
          >
            {t("newRule")}
          </Button>
        }
      >
        {total === 0 && !rulesQuery.isLoading ? (
          <Text c="dimmed" size="sm" ta="center" py="xl">
            {total === 0 && (rulesQuery.data?.items ?? []).length === 0
              ? t("empty")
              : t("noMatches")}
          </Text>
        ) : (
          <ParseRulesTable
            items={pageItems}
            isLoading={rulesQuery.isLoading}
            totalPages={Math.max(1, Math.ceil(total / PAGE_SIZE))}
            page={search.page}
            onPageChange={(page) => patchSearch({ page })}
            onEdit={(rule) => {
              setEditing(rule);
              setEditForm(ruleFormFromResponse(rule));
            }}
            libraryNames={libraryNames}
          />
        )}
      </BrowsePageShell>

      <Modal
        opened={createOpen}
        onClose={() => setCreateOpen(false)}
        title={t("newRule")}
        size="lg"
        centered
      >
        <ParseRuleFormFields
          value={createForm}
          onChange={setCreateForm}
          libraryOptions={libraryOptions}
        />
        <Group justify="flex-end" mt="md">
          <Button variant="default" onClick={() => setCreateOpen(false)}>
            {t("common:actions.cancel")}
          </Button>
          <Button
            loading={createMutation.isPending}
            disabled={!ruleFormCanSubmit(createForm)}
            onClick={() => createMutation.mutate({ body: ruleFormToBody(createForm) })}
          >
            {t("common:actions.save")}
          </Button>
        </Group>
      </Modal>

      <Modal
        opened={editing != null}
        onClose={() => setEditing(null)}
        title={t("editRule")}
        size="lg"
        centered
      >
        <ParseRuleFormFields
          value={editForm}
          onChange={setEditForm}
          libraryOptions={libraryOptions}
        />
        <Group justify="flex-end" mt="md">
          <Button variant="default" onClick={() => setEditing(null)}>
            {t("common:actions.cancel")}
          </Button>
          <Button
            loading={updateMutation.isPending}
            disabled={editing == null || !ruleFormCanSubmit(editForm)}
            onClick={() => {
              if (editing == null) {
                return;
              }
              updateMutation.mutate({
                path: { rule_id: editing.id },
                body: ruleFormToBody(editForm),
              });
            }}
          >
            {t("common:actions.save")}
          </Button>
        </Group>
      </Modal>
    </>
  );
}
