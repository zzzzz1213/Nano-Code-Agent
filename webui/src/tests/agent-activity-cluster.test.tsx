import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AgentActivityCluster } from "@/components/thread/AgentActivityCluster";
import type { UIMessage } from "@/lib/types";

const testState = vi.hoisted(() => ({
  applyRecoveryActionMock: vi.fn(),
  optionalClient: null as null | {
    token: string;
    modelName: string | null;
    client: { defaultChatId: string; sendMessage: ReturnType<typeof vi.fn> };
  },
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    applyRecoveryAction: testState.applyRecoveryActionMock,
  };
});

vi.mock("@/providers/ClientProvider", async () => {
  const actual =
    await vi.importActual<typeof import("@/providers/ClientProvider")>(
      "@/providers/ClientProvider",
    );
  return {
    ...actual,
    useOptionalClient: () => testState.optionalClient,
  };
});

async function loadApiMocks() {
  return vi.importMock<typeof import("@/lib/api")>("@/lib/api");
}

function activityMessages(
  extraReasoning = "",
  extraTool?: UIMessage,
): UIMessage[] {
  const rows: UIMessage[] = [
    {
      id: "r1",
      role: "assistant",
      content: "",
      reasoning: `thinking${extraReasoning}`,
      reasoningStreaming: true,
      isStreaming: true,
      createdAt: 1,
    },
    {
      id: "t1",
      role: "tool",
      kind: "trace",
      content: "search()",
      traces: ["search()"],
      createdAt: 2,
    },
  ];
  if (extraTool) rows.push(extraTool);
  return rows;
}

function installAnimationFrameQueue() {
  const originalRequest = window.requestAnimationFrame;
  const originalCancel = window.cancelAnimationFrame;
  const callbacks = new Map<number, FrameRequestCallback>();
  let nextId = 1;

  window.requestAnimationFrame = ((callback: FrameRequestCallback) => {
    const id = nextId;
    nextId += 1;
    callbacks.set(id, callback);
    return id;
  }) as typeof window.requestAnimationFrame;
  window.cancelAnimationFrame = ((id: number) => {
    callbacks.delete(id);
  }) as typeof window.cancelAnimationFrame;

  return {
    flush() {
      const pending = Array.from(callbacks.entries());
      callbacks.clear();
      for (const [, callback] of pending) callback(0);
    },
    restore() {
      window.requestAnimationFrame = originalRequest;
      window.cancelAnimationFrame = originalCancel;
    },
  };
}

function setScrollGeometry(
  element: HTMLElement,
  geometry: { scrollHeight: number; clientHeight: number; scrollTop?: number },
) {
  Object.defineProperties(element, {
    scrollHeight: { configurable: true, value: geometry.scrollHeight },
    clientHeight: { configurable: true, value: geometry.clientHeight },
    scrollTop: {
      configurable: true,
      value: geometry.scrollTop ?? element.scrollTop,
      writable: true,
    },
  });
}

function installReducedMotion() {
  const original = window.matchMedia;
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: () => ({
      matches: true,
      media: "(prefers-reduced-motion: reduce)",
      addEventListener: () => {},
      removeEventListener: () => {},
    }),
  });
  return () => {
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: original,
    });
  };
}

describe("AgentActivityCluster", () => {
  beforeEach(() => {
    cleanup();
    testState.optionalClient = null;
    testState.applyRecoveryActionMock.mockReset();
  });

  it("submits recovery review actions with the real session key", async () => {
    testState.optionalClient = {
      token: "tok",
      modelName: null,
      client: {
        defaultChatId: "chat-1",
        sendMessage: vi.fn(),
      },
    };
    await loadApiMocks();
    testState.applyRecoveryActionMock.mockResolvedValue({
      ok: true,
      checkpoint: {},
    });
    render(
      <AgentActivityCluster
        messages={[
          {
            id: "checkpoint",
            role: "tool",
            kind: "trace",
            content: "",
            traces: [],
            checkpoint: {
              version: 1,
              turn_id: "turn-1",
              phase: "tools_completed",
              review_required_tool_count: 1,
              needs_input_tool_count: 1,
              recovery_review_items: [
                {
                  tool_call_id: "call-shell",
                  name: "exec",
                  group: "review_required",
                  action_label: "Review before retry",
                  review_state: "awaiting_review",
                  status_label: "Waiting for confirmation",
                },
                {
                  tool_call_id: "call-input",
                  name: "mcp_fetch_context",
                  group: "needs_input",
                  action_label: "Collect input",
                  review_state: "awaiting_input",
                  status_label: "Waiting for input",
                  input_placeholder: "Provide the missing query details",
                },
              ],
              updated_at: "2026-05-21T00:00:00+00:00",
            },
            createdAt: 3,
          },
        ]}
        isTurnStreaming={false}
        hasBodyBelow={false}
        sessionKey="websocket:chat-1"
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /0 tool calls/i }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm retry" }));

    await waitFor(() => {
      expect(testState.applyRecoveryActionMock).toHaveBeenCalledWith("tok", {
        sessionKey: "websocket:chat-1",
        toolCallId: "call-shell",
        action: "confirm_retry",
        userInput: undefined,
      });
    });
    expect(screen.getByText("Retry confirmed")).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText("Provide the missing query details"), {
      target: { value: "use latest logs" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Submit input" }));

    await waitFor(() => {
      expect(testState.applyRecoveryActionMock).toHaveBeenNthCalledWith(2, "tok", {
        sessionKey: "websocket:chat-1",
        toolCallId: "call-input",
        action: "provide_input",
        userInput: "use latest logs",
      });
    });
    expect(screen.getByText("Input collected")).toBeInTheDocument();
  });

  it("shows local validation when a needs-input action is submitted empty", async () => {
    testState.optionalClient = {
      token: "tok",
      modelName: null,
      client: {
        defaultChatId: "chat-1",
        sendMessage: vi.fn(),
      },
    };
    render(
      <AgentActivityCluster
        messages={[
          {
            id: "checkpoint",
            role: "tool",
            kind: "trace",
            content: "",
            traces: [],
            checkpoint: {
              version: 1,
              turn_id: "turn-1",
              phase: "tools_completed",
              needs_input_tool_count: 1,
              recovery_review_items: [
                {
                  tool_call_id: "call-input",
                  name: "mcp_fetch_context",
                  group: "needs_input",
                  action_label: "Collect input",
                  review_state: "awaiting_input",
                  input_placeholder: "Provide missing input for mcp_fetch_context",
                },
              ],
              updated_at: "2026-05-21T00:00:00+00:00",
            },
            createdAt: 3,
          },
        ]}
        isTurnStreaming={false}
        hasBodyBelow={false}
        sessionKey="websocket:chat-1"
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /0 tool calls/i }));
    fireEvent.click(screen.getByRole("button", { name: "Submit input" }));

    expect(screen.getByText("Input required")).toBeInTheDocument();
    expect(testState.applyRecoveryActionMock).not.toHaveBeenCalled();
  });

  it("jumps to the latest activity when opened", () => {
    const raf = installAnimationFrameQueue();
    try {
      render(
        <AgentActivityCluster
          messages={activityMessages()}
          isTurnStreaming
          hasBodyBelow={false}
        />,
      );

      fireEvent.click(screen.getByRole("button", { name: /tools/i }));
      const scrollport = screen.getByTestId("agent-activity-scroll");
      setScrollGeometry(scrollport, {
        scrollHeight: 1000,
        clientHeight: 120,
        scrollTop: 0,
      });

      act(() => {
        raf.flush();
      });

      expect(scrollport.scrollTop).toBe(880);
    } finally {
      raf.restore();
    }
  });

  it("follows new reasoning and tool activity while the user is at the bottom", () => {
    const raf = installAnimationFrameQueue();
    try {
      const { rerender } = render(
        <AgentActivityCluster
          messages={activityMessages()}
          isTurnStreaming
          hasBodyBelow={false}
        />,
      );

      fireEvent.click(screen.getByRole("button", { name: /tools/i }));
      const scrollport = screen.getByTestId("agent-activity-scroll");
      setScrollGeometry(scrollport, {
        scrollHeight: 1000,
        clientHeight: 120,
        scrollTop: 0,
      });
      act(() => {
        raf.flush();
      });

      rerender(
        <AgentActivityCluster
          messages={activityMessages(" with more detail", {
            id: "t2",
            role: "tool",
            kind: "trace",
            content: "open_browser()",
            traces: ["open_browser()"],
            createdAt: 3,
          })}
          isTurnStreaming
          hasBodyBelow={false}
        />,
      );
      setScrollGeometry(scrollport, {
        scrollHeight: 1500,
        clientHeight: 120,
        scrollTop: scrollport.scrollTop,
      });

      act(() => {
        raf.flush();
      });

      expect(scrollport.scrollTop).toBe(1380);
    } finally {
      raf.restore();
    }
  });

  it("does not pull the user down after they scroll up inside the activity pane", () => {
    const raf = installAnimationFrameQueue();
    try {
      const { rerender } = render(
        <AgentActivityCluster
          messages={activityMessages()}
          isTurnStreaming
          hasBodyBelow={false}
        />,
      );

      fireEvent.click(screen.getByRole("button", { name: /tools/i }));
      const scrollport = screen.getByTestId("agent-activity-scroll");
      setScrollGeometry(scrollport, {
        scrollHeight: 1000,
        clientHeight: 120,
        scrollTop: 0,
      });
      act(() => {
        raf.flush();
      });

      scrollport.scrollTop = 100;
      fireEvent.scroll(scrollport);

      rerender(
        <AgentActivityCluster
          messages={activityMessages(" still streaming")}
          isTurnStreaming
          hasBodyBelow={false}
        />,
      );
      setScrollGeometry(scrollport, {
        scrollHeight: 1500,
        clientHeight: 120,
        scrollTop: scrollport.scrollTop,
      });

      act(() => {
        raf.flush();
      });

      expect(scrollport.scrollTop).toBe(100);
    } finally {
      raf.restore();
    }
  });

  it("renders file edit totals and a compact expanded file list", async () => {
    const restoreMotion = installReducedMotion();
    try {
      render(
        <AgentActivityCluster
          messages={activityMessages("", {
            id: "t2",
            role: "tool",
            kind: "trace",
            content: "edit_file()",
            traces: ["edit_file()"],
            fileEdits: [
              {
                call_id: "call-edit",
                tool: "edit_file",
                path: "src/app.tsx",
                absolute_path: "/Users/renxubin/project/src/app.tsx",
                phase: "end",
                added: 12,
                deleted: 3,
                approximate: false,
                status: "done",
              },
            ],
            createdAt: 3,
          })}
          isTurnStreaming={false}
          hasBodyBelow={false}
        />,
      );

      expect(
        screen.getByRole("button", { name: /editing/i }),
      ).toBeInTheDocument();
      expect(
        screen.getByTestId("activity-header-file-reference"),
      ).toHaveTextContent("app.tsx");
      expect(
        screen.getByTestId("activity-header-file-reference"),
      ).toHaveAttribute("aria-label", "/Users/renxubin/project/src/app.tsx");
      fireEvent.click(screen.getByRole("button", { name: /editing/i }));

      expect(screen.getByText("Tool steps")).toBeInTheDocument();
      expect(screen.getByText("File changes")).toBeInTheDocument();
      const fileRef = screen.getByTestId("activity-file-reference");
      expect(fileRef).toHaveTextContent("src/app.tsx");
      expect(fileRef).toHaveAttribute(
        "aria-label",
        "/Users/renxubin/project/src/app.tsx",
      );
      await waitFor(() => {
        expect(screen.getAllByText("+12").length).toBeGreaterThan(0);
        expect(screen.getAllByText("-3").length).toBeGreaterThan(0);
      });
    } finally {
      restoreMotion();
    }
  });

  it("renders pending file edit placeholders before the path is known", () => {
    render(
      <AgentActivityCluster
        messages={activityMessages("", {
          id: "t2",
          role: "tool",
          kind: "trace",
          content: "",
          traces: [],
          fileEdits: [
            {
              call_id: "call-edit",
              tool: "edit_file",
              path: "",
              phase: "start",
              added: 0,
              deleted: 0,
              approximate: true,
              status: "editing",
              pending: true,
            },
          ],
          createdAt: 3,
        })}
        isTurnStreaming
        hasBodyBelow={false}
      />,
    );

    expect(
      screen.getByRole("button", { name: /editing/i }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /editing/i }));
    expect(screen.getByText("Preparing file edit…")).toBeInTheDocument();
  });

  it("groups file changes by area and labels inferred change types", async () => {
    const restoreMotion = installReducedMotion();
    try {
      render(
        <AgentActivityCluster
          messages={activityMessages("", {
            id: "t2",
            role: "tool",
            kind: "trace",
            content: "edit_file()",
            traces: ["edit_file()"],
            fileEdits: [
              {
                call_id: "call-edit-front",
                tool: "edit_file",
                path: "webui/src/App.tsx",
                phase: "end",
                added: 5,
                deleted: 2,
                status: "done",
              },
              {
                call_id: "call-edit-back",
                tool: "edit_file",
                path: "nanobot/agent/runner.py",
                phase: "end",
                added: 0,
                deleted: 9,
                status: "done",
              },
              {
                call_id: "call-edit-docs",
                tool: "edit_file",
                path: "docs/usage.md",
                phase: "end",
                added: 4,
                deleted: 0,
                status: "done",
              },
            ],
            createdAt: 3,
          })}
          isTurnStreaming={false}
          hasBodyBelow={false}
        />,
      );

      expect(
        screen.getByRole("button", { name: /editing.*3 files/i }),
      ).toBeInTheDocument();
      fireEvent.click(
        screen.getByRole("button", { name: /editing.*3 files/i }),
      );

      expect(
        screen.getByLabelText("Engineering activity timeline"),
      ).toBeInTheDocument();
      expect(screen.getByLabelText("Task snapshot")).toBeInTheDocument();
      expect(screen.getByText("Task snapshot")).toBeInTheDocument();
      expect(screen.getByText("Phase")).toBeInTheDocument();
      expect(screen.getAllByText("Tools").length).toBeGreaterThan(0);
      expect(screen.getAllByText("Files").length).toBeGreaterThan(0);
      expect(screen.getAllByText("Checks").length).toBeGreaterThan(0);
      expect(screen.getByText("Rebuilt from history")).toBeInTheDocument();
      expect(screen.getAllByText("Reading").length).toBeGreaterThan(0);
      expect(screen.getAllByText("Tools").length).toBeGreaterThan(0);
      expect(screen.getAllByText("Editing").length).toBeGreaterThan(0);
      expect(screen.getAllByText("Checking").length).toBeGreaterThan(0);
      expect(screen.getAllByText("Done").length).toBeGreaterThan(0);
      expect(screen.getAllByText("Frontend").length).toBeGreaterThan(0);
      expect(screen.getAllByText("Backend").length).toBeGreaterThan(0);
      expect(screen.getAllByText("Docs").length).toBeGreaterThan(0);
      expect(screen.getAllByText("Modified").length).toBeGreaterThan(0);
      expect(screen.getAllByText("Deleted").length).toBeGreaterThan(0);
      expect(screen.getAllByText("Added").length).toBeGreaterThan(0);
      expect(screen.getByText("tsx")).toBeInTheDocument();
      expect(screen.getByText("py")).toBeInTheDocument();
      expect(screen.getByText("md")).toBeInTheDocument();
      await waitFor(() => {
        expect(screen.getAllByText("+9").length).toBeGreaterThan(0);
        expect(screen.getAllByText("-11").length).toBeGreaterThan(0);
      });
    } finally {
      restoreMotion();
    }
  });

  it("groups test and build commands as checks", () => {
    render(
      <AgentActivityCluster
        messages={activityMessages("", {
          id: "t2",
          role: "tool",
          kind: "trace",
          content: 'exec({"command":"pytest tests/agent -q"})',
          traces: [
            'read_file({"path":"nanobot/agent/runner.py"})',
            'exec({"command":"pytest tests/agent -q"})',
          ],
          createdAt: 3,
        })}
        isTurnStreaming={false}
        hasBodyBelow={false}
      />,
    );

    expect(
      screen.getByRole("button", { name: /checking.*1 passed/i }),
    ).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: /checking.*1 passed/i }),
    );
    expect(screen.getByText("Tool steps")).toBeInTheDocument();
    expect(screen.getAllByText("Checks").length).toBeGreaterThan(0);
    expect(screen.getByText("Read")).toBeInTheDocument();
    expect(screen.getByText("Check")).toBeInTheDocument();
    expect(screen.getAllByText("pytest tests/agent -q").length).toBeGreaterThan(0);
  });

  it("uses backend checkpoint data for task snapshots when traces are sparse", () => {
    render(
      <AgentActivityCluster
        messages={[
          {
            id: "checkpoint",
            role: "tool",
            kind: "trace",
            content: "",
            traces: [],
            checkpoint: {
              version: 1,
              turn_id: "turn-1",
              phase: "tools_completed",
              tool_call_count: 2,
              last_tool_call_id: "call-build",
              file_edit_count: 1,
              check_state: "passed",
              updated_at: "2026-05-21T00:00:00+00:00",
            },
            createdAt: 3,
          },
        ]}
        isTurnStreaming={false}
        hasBodyBelow={false}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /0 tool calls/i }));
    expect(screen.getByLabelText("Task snapshot")).toBeInTheDocument();
    expect(screen.getByText(/Tools · Tool results saved/)).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByText("1 · +0 -0")).toBeInTheDocument();
    expect(screen.getAllByText("1 passed").length).toBeGreaterThan(0);
    expect(screen.getByText("Rebuilt from history")).toBeInTheDocument();
  });

  it("renders workbench cards for task plan, checks, and turn summary", () => {
    render(
      <AgentActivityCluster
        messages={activityMessages("", {
          id: "t-workbench",
          role: "tool",
          kind: "trace",
          content: "",
          traces: [],
          toolEvents: [
            {
              phase: "end",
              call_id: "call-check",
              name: "exec",
              arguments: { command: "pytest tests/agent -q" },
              result:
                "================= 1 failed, 2 passed in 0.42s =================\nExit code: 1",
              failure_category: "tool_exception",
              diagnostic_label: "Test failure",
              diagnostic_hint:
                "The latest pytest run still has a failing assertion.",
              recommended_action:
                "Rerun the targeted pytest command after fixing the failing test.",
            },
          ],
          checkpoint: {
            version: 1,
            turn_id: "turn-workbench",
            phase: "tools_completed",
            tool_call_count: 3,
            file_edit_count: 1,
            check_state: "failed",
            updated_at: "2026-05-21T00:00:00+00:00",
          },
          fileEdits: [
            {
              call_id: "call-edit",
              tool: "edit_file",
              path: "nanobot/agent/runner.py",
              phase: "end",
              added: 180,
              deleted: 60,
              status: "done",
              approximate: true,
            },
            {
              call_id: "call-binary",
              tool: "write_file",
              path: "docs/architecture.png",
              phase: "end",
              added: 0,
              deleted: 0,
              status: "done",
              binary: true,
            },
          ],
          createdAt: 3,
        })}
        isTurnStreaming={false}
        hasBodyBelow={false}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /checking.*1 failed/i }));
    const checkResults = screen.getByLabelText("Check results");
    const diffPreview = screen.getByLabelText("Diff preview");
    const turnSummary = screen.getByLabelText("Turn summary");
    expect(screen.getByText("Task plan")).toBeInTheDocument();
    expect(screen.getByText("Check results")).toBeInTheDocument();
    expect(screen.getByText("Diff preview")).toBeInTheDocument();
    expect(screen.getByText("Turn summary")).toBeInTheDocument();
    expect(screen.getByLabelText("Task plan")).toBeInTheDocument();
    expect(checkResults).toBeInTheDocument();
    expect(diffPreview).toBeInTheDocument();
    expect(turnSummary).toBeInTheDocument();
    expect(screen.getByText("Resolve failing checks")).toBeInTheDocument();
    expect(
      within(screen.getByLabelText("Task plan")).getByRole("button", {
        name: "Open checks",
      }),
    ).toBeInTheDocument();
    expect(
      within(checkResults).getAllByText("pytest tests/agent -q").length,
    ).toBeGreaterThan(0);
    expect(within(checkResults).getByText("Tool exception")).toBeInTheDocument();
    expect(within(checkResults).getByText("Test failure")).toBeInTheDocument();
    expect(within(checkResults).getByText(/1 failed, 2 passed/i)).toBeInTheDocument();
    expect(
      within(checkResults).getByText(/failing assertion/i),
    ).toBeInTheDocument();
    expect(
      within(checkResults).getByText(/Rerun the targeted pytest command/i),
    ).toBeInTheDocument();
    expect(within(diffPreview).getByText("nanobot/agent/runner.py")).toBeInTheDocument();
    expect(within(diffPreview).getByText("docs/architecture.png")).toBeInTheDocument();
    expect(within(diffPreview).getByText("Large change")).toBeInTheDocument();
    expect(within(diffPreview).getByText("Binary")).toBeInTheDocument();
    expect(within(turnSummary).getByText("Edited 2 files (+180 -60).")).toBeInTheDocument();
    expect(
      within(turnSummary).getByText(
        "Inspect the failing check summary and rerun the focused command.",
      ),
    ).toBeInTheDocument();
    expect(
      within(turnSummary).getByRole("button", { name: "Open checks" }),
    ).toBeInTheDocument();
  });

  it("renders context compaction snapshots distinctly", () => {
    render(
      <AgentActivityCluster
        messages={[
          {
            id: "ctx1",
            role: "tool",
            kind: "trace",
            content: "",
            traces: [],
            contextCompaction: {
              version: 1,
              reason: "token_budget",
              source: "token_consolidator",
              before_message_count: 40,
              after_message_count: 12,
              archived_message_count: 28,
              kept_message_count: 12,
              before_token_estimate: 9000,
              after_token_estimate: 2600,
              saved_token_estimate: 6400,
              summary_token_estimate: 180,
              summary_preview:
                "Kept the implementation decisions and open test failures.",
              summary_sections: {
                overview: ["Kept the implementation decisions"],
                decisions: ["Retain the current command routing path"],
              },
              updated_at: "2026-05-21T00:00:00",
            },
            createdAt: 1,
          },
        ]}
        isTurnStreaming={false}
        hasBodyBelow={false}
      />,
    );

    expect(
      screen.getByRole("button", { name: /context compressed/i }),
    ).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: /context compressed/i }),
    );

    expect(screen.getByText("Context memory")).toBeInTheDocument();
    expect(screen.getAllByText("Context compressed").length).toBeGreaterThan(0);
    expect(screen.getByText("Token budget")).toBeInTheDocument();
    expect(screen.getByText("-6400")).toBeInTheDocument();
    expect(screen.getByText("28 archived · 12 kept")).toBeInTheDocument();
    expect(
      screen.getAllByText(/implementation decisions/).length,
    ).toBeGreaterThan(0);
    expect(
      screen.getAllByText(/Overview: Kept the implementation decisions/).length,
    ).toBeGreaterThan(0);
  });

  it("renders memory source snapshots without exposing raw memory text", () => {
    render(
      <AgentActivityCluster
        messages={[
          {
            id: "mem1",
            role: "tool",
            kind: "trace",
            content: "",
            traces: [],
            memorySnapshot: {
              version: 1,
              sources: {
                memory: {
                  included: true,
                  token_estimate: 120,
                  char_count: 400,
                },
                user: { included: true, token_estimate: 40, char_count: 100 },
                soul: { included: false, token_estimate: 0 },
                recent_history: { included: true, entry_count: 3 },
                session_summary: { included: false, token_estimate: 0 },
              },
              retrieved: {
                included: true,
                entry_count: 2,
                categories: {
                  decision: 1,
                  failure: 1,
                },
                reasons: ["path:api_gateway.py", "section:failures"],
                items: [
                  {
                    id: "doc-decision",
                    source: "websocket:thread-1",
                    category: "decision",
                    reason: "path:api_gateway.py",
                    safety: "read-only",
                  },
                  {
                    id: "doc-failure",
                    source: "websocket:thread-2",
                    category: "failure",
                    reason: "section:failures",
                    safety: "requires_confirmation",
                  },
                ],
              },
              updated_at: "2026-05-21T00:00:00",
            },
            createdAt: 1,
          },
        ]}
        isTurnStreaming={false}
        hasBodyBelow={false}
      />,
    );

    expect(
      screen.getByRole("button", { name: /memory snapshot/i }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /memory snapshot/i }));

    expect(screen.getAllByText("Memory snapshot").length).toBeGreaterThan(0);
    expect(screen.getByText("Memory sources loaded")).toBeInTheDocument();
    expect(screen.getByText("3 active")).toBeInTheDocument();
    expect(screen.getByText("Project memory")).toBeInTheDocument();
    expect(screen.getByText("User profile")).toBeInTheDocument();
    expect(screen.getByText("Recent history")).toBeInTheDocument();
    expect(screen.getByText("3 entries")).toBeInTheDocument();
    expect(screen.getByText("120 tokens")).toBeInTheDocument();
    expect(screen.getByText("Retrieved metadata")).toBeInTheDocument();
    expect(screen.getByText("Retrieved 2")).toBeInTheDocument();
    expect(screen.getByText("Decision 1")).toBeInTheDocument();
    expect(screen.getByText("Failure 1")).toBeInTheDocument();
    expect(screen.getByText("path:api_gateway.py")).toBeInTheDocument();
    expect(screen.queryByText(/retry adapter raw body/i)).not.toBeInTheDocument();
  });

  it("renders active skill observability without exposing skill bodies", () => {
    render(
      <AgentActivityCluster
        messages={[
          {
            id: "skills1",
            role: "tool",
            kind: "trace",
            content: "",
            traces: [],
            activeSkills: {
              version: 1,
              skills: [
                {
                  name: "coding-assistant",
                  source: "always",
                  reason: "always enabled",
                },
                {
                  name: "test-fix",
                  source: "auto",
                  matched_keywords: ["pytest", "failed"],
                  priority: 70,
                  reason: "matched: pytest, failed",
                },
              ],
            },
            createdAt: 1,
          },
        ]}
        isTurnStreaming={false}
        hasBodyBelow={false}
      />,
    );

    expect(
      screen.getByRole("button", { name: /active skills/i }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /active skills/i }));

    expect(screen.getAllByText("Active skills").length).toBeGreaterThan(0);
    expect(screen.getByText("coding-assistant")).toBeInTheDocument();
    expect(screen.getAllByText("test-fix").length).toBeGreaterThan(0);
    expect(screen.getByText("always")).toBeInTheDocument();
    expect(screen.getByText("auto")).toBeInTheDocument();
    expect(screen.getByText("matched: pytest, failed")).toBeInTheDocument();
    expect(screen.queryByText(/# Test Fix/i)).not.toBeInTheDocument();
  });

  it("renders memory candidates with an explicit save action", () => {
    render(
      <AgentActivityCluster
        messages={[
          {
            id: "memcand1",
            role: "tool",
            kind: "trace",
            content: "",
            traces: [],
            memoryCandidate: {
              version: 1,
              id: "memcand_1",
              type: "user_profile",
              target: "USER.md",
              title: "User Profile",
              content: "I prefer concise replies",
              reason: "User preference or profile detail",
            },
            createdAt: 1,
          },
        ]}
        isTurnStreaming={false}
        hasBodyBelow={false}
      />,
    );

    expect(
      screen.getByRole("button", { name: /memory candidate/i }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /memory candidate/i }));

    expect(screen.getAllByText("Memory candidate").length).toBeGreaterThan(0);
    expect(screen.getByText("User Profile")).toBeInTheDocument();
    expect(screen.getByText("I prefer concise replies")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Save memory candidate" }),
    ).toBeDisabled();
  });

  it("marks recovered checkpoint snapshots distinctly", () => {
    const onResumeSafeTools = vi.fn();
    render(
      <AgentActivityCluster
        messages={[
          {
            id: "recovered-checkpoint",
            role: "tool",
            kind: "trace",
            content: "",
            traces: [],
            checkpoint: {
              version: 1,
              turn_id: "turn-restore",
              phase: "awaiting_tools",
              tool_call_count: 1,
              last_tool_call_id: "call-test",
              file_edit_count: 0,
              check_state: "running",
              source: "recovered",
              recovered: true,
              recovered_pending_tool_count: 1,
              reused_tool_count: 1,
              compensation_tool_count: 1,
              retryable_tool_count: 1,
              requires_user_tool_count: 1,
              resumable_tool_count: 1,
              safe_resume_tool_count: 1,
              review_required_tool_count: 1,
              needs_input_tool_count: 1,
              blocked_tool_count: 1,
              recovery_review_items: [
                {
                  tool_call_id: "call-read",
                  name: "read_file",
                  group: "safe_resume",
                  reason: "read_only_safe_candidate",
                  recovery_action: "resume_safe",
                  action_label: "Resume safe tools",
                  review_kind: "read_only",
                  summary: "path: nanobot/agent/runner.py",
                  scope: "core",
                  can_resume_now: true,
                },
                {
                  tool_call_id: "call-shell",
                  name: "exec",
                  group: "review_required",
                  reason: "shell_command_requires_review",
                  recovery_action: "review_before_retry",
                  action_label: "Review before retry",
                  review_kind: "shell",
                  summary: "command available during review",
                  scope: "core",
                },
                {
                  tool_call_id: "call-input",
                  name: "mcp_fetch_context",
                  group: "needs_input",
                  reason: "requires_user_input",
                  recovery_action: "provide_input",
                  action_label: "Collect input",
                  review_kind: "needs_input",
                  summary: "query: current build error",
                  scope: "mcp",
                },
                {
                  tool_call_id: "call-blocked",
                  name: "write_file",
                  group: "blocked",
                  reason: "blocked_by_safety_policy",
                  recovery_action: "revise_request",
                  action_label: "Revise request",
                  review_kind: "blocked",
                  summary: "path: .env",
                  scope: "core",
                },
              ],
              recovery_review_count: 4,
              updated_at: "2026-05-21T00:00:00+00:00",
            },
            createdAt: 3,
          },
        ]}
        isTurnStreaming={false}
        hasBodyBelow={false}
        onResumeSafeTools={onResumeSafeTools}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /0 tool calls/i }));
    expect(screen.getByLabelText("Task snapshot")).toBeInTheDocument();
    expect(screen.getByLabelText("Task plan")).toBeInTheDocument();
    expect(screen.getByText("Recovered checkpoint")).toBeInTheDocument();
    expect(screen.getByText("Reused 1")).toBeInTheDocument();
    expect(screen.getByText("Compensated 1")).toBeInTheDocument();
    expect(screen.getByText("Retryable 1")).toBeInTheDocument();
    expect(screen.getByText("Resumable 1")).toBeInTheDocument();
    expect(screen.getByText("Review 1")).toBeInTheDocument();
    expect(screen.getByText("Blocked 1")).toBeInTheDocument();
    expect(
      screen.getByText("Review before retry: 1 need review, 1 need input, 1 blocked."),
    ).toBeInTheDocument();
    expect(screen.getByText("Review recovery actions")).toBeInTheDocument();
    expect(screen.getByText("4 pending items")).toBeInTheDocument();
    expect(
      within(screen.getByLabelText("Task plan")).getByRole("button", {
        name: "Open review",
      }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Recovery review")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Resume safe tools" }),
    ).toBeInTheDocument();
    expect(screen.getByText("read_file")).toBeInTheDocument();
    expect(screen.getByText("Safe resume")).toBeInTheDocument();
    expect(screen.getByText("path: nanobot/agent/runner.py")).toBeInTheDocument();
    expect(screen.getByText("Read only safe candidate")).toBeInTheDocument();
    expect(screen.getByText("exec")).toBeInTheDocument();
    expect(screen.getByText("Shell command requires review")).toBeInTheDocument();
    expect(screen.getByText("mcp_fetch_context")).toBeInTheDocument();
    expect(screen.getByText("Collect input")).toBeInTheDocument();
    expect(screen.getByText("write_file")).toBeInTheDocument();
    expect(screen.getByText("Blocked by safety policy")).toBeInTheDocument();
    expect(screen.getByText("Needs input 1")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Resume safe tools" }));
    expect(onResumeSafeTools).toHaveBeenCalledTimes(1);
    expect(screen.getAllByText("Running").length).toBeGreaterThan(0);
  });

  it("shows check command status and result summaries", () => {
    render(
      <AgentActivityCluster
        messages={activityMessages("", {
          id: "t2",
          role: "tool",
          kind: "trace",
          content: 'exec({"command":"pytest tests/agent -q"})',
          traces: ['exec({"command":"pytest tests/agent -q"})'],
          toolEvents: [
            {
              phase: "end",
              call_id: "call-check",
              name: "exec",
              arguments: { command: "pytest tests/agent -q" },
              result:
                "================= 1 failed, 2 passed in 0.42s =================\nExit code: 1",
            },
          ],
          createdAt: 3,
        })}
        isTurnStreaming={false}
        hasBodyBelow={false}
      />,
    );

    expect(
      screen.getByRole("button", { name: /checking.*1 failed/i }),
    ).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: /checking.*1 failed/i }),
    );
    const checkResults = screen.getByLabelText("Check results");
    expect(screen.getByLabelText("Task snapshot")).toBeInTheDocument();
    expect(screen.getByText("Needs attention")).toBeInTheDocument();
    expect(screen.getAllByText("1 failed").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Failed").length).toBeGreaterThan(0);
    expect(within(checkResults).getByText(/1 failed, 2 passed/)).toBeInTheDocument();
  });

  it("shows structured tool risk and blocked shell labels", () => {
    render(
      <AgentActivityCluster
        messages={activityMessages("", {
          id: "t-risk",
          role: "tool",
          kind: "trace",
          content: "",
          traces: [],
          toolEvents: [
            {
              phase: "error",
              call_id: "call-risk",
              name: "exec",
              arguments: { command: "rm -rf /tmp/build" },
              error:
                "Error: Command blocked by deny pattern filter (dangerous shell command: recursive delete; risk=shell/high)",
              risk_category: "shell",
              risk_level: "high",
              safety: { category: "shell", level: "high", blocked: true },
              failure_category: "safety_block",
              recovery_action: "revise_request",
              retryable: false,
              needs_user_input: true,
            },
          ],
          createdAt: 3,
        })}
        isTurnStreaming={false}
        hasBodyBelow={false}
      />,
    );

    expect(screen.getByRole("button", { name: /tools/i })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /tools/i }));
    expect(screen.getByText("Blocked")).toBeInTheDocument();
    expect(screen.getByText("Needs input")).toBeInTheDocument();
    expect(screen.getByText("rm -rf /tmp/build")).toBeInTheDocument();
  });

  it("shows retryable tool recovery labels", () => {
    render(
      <AgentActivityCluster
        messages={activityMessages("", {
          id: "t-retry",
          role: "tool",
          kind: "trace",
          content: "",
          traces: [],
          toolEvents: [
            {
              phase: "error",
              call_id: "call-retry",
              name: "exec",
              arguments: { command: "npm run test" },
              error: "Error: process exited unexpectedly",
              risk_category: "shell",
              risk_level: "high",
              failure_category: "tool_exception",
              recovery_action: "retry",
              retryable: true,
              needs_user_input: false,
            },
          ],
          createdAt: 3,
        })}
        isTurnStreaming={false}
        hasBodyBelow={false}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /checking/i }));
    expect(screen.getByText("Retryable")).toBeInTheDocument();
    expect(screen.getAllByText("npm run test").length).toBeGreaterThan(0);
  });

  it("shows MCP diagnostic labels and suggestions", () => {
    render(
      <AgentActivityCluster
        messages={activityMessages("", {
          id: "t-mcp-diagnostic",
          role: "tool",
          kind: "trace",
          content: "",
          traces: [],
          toolEvents: [
            {
              phase: "error",
              call_id: "call-mcp-protocol",
              name: "mcp_docs_lookup",
              arguments: { query: "runner retry policy" },
              error: "(MCP tool call failed: JSONRPC protocol error [RuntimeError: invalid json])",
              risk_category: "mcp",
              risk_level: "medium",
              failure_category: "mcp_protocol_error",
              recovery_action: "revise_request",
              retryable: false,
              needs_user_input: true,
              diagnostic_label: "Protocol error",
              diagnostic_hint:
                "The MCP server returned malformed JSON-RPC or polluted stdout.",
              recommended_action:
                "Check the MCP server logs and ensure protocol output stays on stdout only.",
            },
          ],
          createdAt: 3,
        })}
        isTurnStreaming={false}
        hasBodyBelow={false}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /tools/i }));
    expect(screen.getByText("Protocol error")).toBeInTheDocument();
    expect(
      screen.getByText(/The MCP server returned malformed JSON-RPC or polluted stdout\./),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/ensure protocol output stays on stdout only\./),
    ).toBeInTheDocument();
  });

  it("shows queued and running tool scheduling metadata", () => {
    render(
      <AgentActivityCluster
        messages={activityMessages("", {
          id: "t-scheduling",
          role: "tool",
          kind: "trace",
          content: "",
          traces: [],
          toolEvents: [
            {
              phase: "running",
              call_id: "call-read",
              name: "read_file",
              arguments: { path: "nanobot/agent/runner.py" },
              elapsed_ms: 1250,
              batch_id: "chk_1:1",
              batch_index: 1,
              batch_count: 2,
              batch_size: 2,
              concurrency_limit: 2,
              queue_position: 1,
              read_only: true,
              concurrency_safe: true,
              exclusive: false,
              config_key: "filesystem",
              scopes: ["core", "subagent"],
            },
            {
              phase: "queued",
              call_id: "call-grep",
              name: "grep",
              arguments: { pattern: "ToolProgressEvent" },
              batch_id: "chk_1:2",
              batch_index: 2,
              batch_count: 2,
              batch_size: 1,
              concurrency_limit: 2,
              queue_position: 1,
              read_only: true,
              concurrency_safe: true,
              exclusive: false,
              config_key: "search",
              scopes: ["core"],
            },
          ],
          createdAt: 3,
        })}
        isTurnStreaming
        hasBodyBelow={false}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /tools/i }));
    expect(screen.getByTestId("tool-scheduling-summary")).toBeInTheDocument();
    expect(screen.getAllByText("Running").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Queued").length).toBeGreaterThan(0);
    expect(screen.getAllByText("2").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Parallel safe").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Read only").length).toBeGreaterThan(0);
    expect(screen.getByText("Config: filesystem")).toBeInTheDocument();
    expect(screen.getByText("B1/2 · #1 · limit 2")).toBeInTheDocument();
    expect(screen.getByText("Elapsed 1.3s")).toBeInTheDocument();
  });

  it("merges repeated edits for the same path and lets successful edits win over failures", async () => {
    const restoreMotion = installReducedMotion();
    try {
      render(
        <AgentActivityCluster
          messages={activityMessages("", {
            id: "t2",
            role: "tool",
            kind: "trace",
            content: "edit_file()",
            traces: ["edit_file()"],
            fileEdits: [
              {
                call_id: "call-edit-1",
                tool: "edit_file",
                path: "minecraft-fps/index.html",
                phase: "end",
                added: 2,
                deleted: 1,
                approximate: false,
                status: "done",
              },
              {
                call_id: "call-edit-2",
                tool: "edit_file",
                path: "minecraft-fps/index.html",
                phase: "error",
                added: 0,
                deleted: 0,
                approximate: false,
                status: "error",
                error: "patch failed",
              },
              {
                call_id: "call-edit-3",
                tool: "edit_file",
                path: "minecraft-fps/index.html",
                phase: "end",
                added: 6,
                deleted: 6,
                approximate: false,
                status: "done",
              },
            ],
            createdAt: 3,
          })}
          isTurnStreaming={false}
          hasBodyBelow={false}
        />,
      );

      expect(
        screen.getByRole("button", { name: /editing/i }),
      ).toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: /failed index\.html/i }),
      ).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: /editing/i }));

      const fileRefs = screen.getAllByTestId("activity-file-reference");
      expect(fileRefs).toHaveLength(1);
      expect(fileRefs[0]).toHaveTextContent("minecraft-fps/index.html");
      expect(screen.queryByText("Failed")).not.toBeInTheDocument();
      await waitFor(() => {
        expect(screen.getAllByText("+8").length).toBeGreaterThan(0);
        expect(screen.getAllByText("-7").length).toBeGreaterThan(0);
      });
    } finally {
      restoreMotion();
    }
  });
});
