import {startTransition, useEffect, useMemo, useRef, useState} from 'react';
import {spawn, type ChildProcessWithoutNullStreams} from 'node:child_process';
import readline from 'node:readline';

import type {
	BackendEvent,
	BridgeSessionSnapshot,
	FrontendConfig,
	McpServerSnapshot,
	SelectOptionPayload,
	SwarmNotificationSnapshot,
	SwarmTeammateSnapshot,
	TaskSnapshot,
	TranscriptItem,
} from '../types.js';

const PROTOCOL_PREFIX = 'OHJSON:';
const ASSISTANT_DELTA_FLUSH_MS = 50;
const ASSISTANT_DELTA_FLUSH_CHARS = 384;
const THINKING_DELTA_FLUSH_MS = 200;
const THINKING_DELTA_FLUSH_CHARS = 1200;
const TRANSCRIPT_EVENT_FLUSH_MS = 50;

const stableStringify = (value: unknown): string => JSON.stringify(value);

export function useBackendSession(config: FrontendConfig, onExit: (code?: number | null) => void) {
	const [transcript, setTranscript] = useState<TranscriptItem[]>([]);
	const [assistantBuffer, setAssistantBuffer] = useState('');
	const [thinkingBuffer, setThinkingBuffer] = useState('');
	const [status, setStatus] = useState<Record<string, unknown>>({});
	const [tasks, setTasks] = useState<TaskSnapshot[]>([]);
	const [commands, setCommands] = useState<string[]>([]);
	const [busySafeCommands, setBusySafeCommands] = useState<string[]>([]);
	const [toolCompletions, setToolCompletions] = useState(0);
	const [commandDescriptions, setCommandDescriptions] = useState<Record<string, string>>({});
	const [fileMatches, setFileMatches] = useState<string[]>([]);
	const [mcpServers, setMcpServers] = useState<McpServerSnapshot[]>([]);
	const [bridgeSessions, setBridgeSessions] = useState<BridgeSessionSnapshot[]>([]);
	const [modal, setModal] = useState<Record<string, unknown> | null>(null);
	const [selectRequest, setSelectRequest] = useState<{title: string; command: string; options: SelectOptionPayload[]; searchable?: boolean} | null>(null);
	const [busy, setBusy] = useState(false);
	const [busyLabel, setBusyLabel] = useState<string | undefined>(undefined);
	const [ready, setReady] = useState(false);
	const [todoMarkdown, setTodoMarkdown] = useState('');
	const [swarmTeammates, setSwarmTeammates] = useState<SwarmTeammateSnapshot[]>([]);
	const [swarmNotifications, setSwarmNotifications] = useState<SwarmNotificationSnapshot[]>([]);
	const statusRef = useRef<Record<string, unknown>>({});
	const childRef = useRef<ChildProcessWithoutNullStreams | null>(null);
	const sentInitialPrompt = useRef(false);
	const lastStatusSnapshotRef = useRef('');
	const lastTasksSnapshotRef = useRef('');
	const lastMcpSnapshotRef = useRef('');
	const lastBridgeSnapshotRef = useRef('');

	// Streaming deltas can arrive one token at a time; updating Ink state for each
	// delta causes heavy re-rendering/flicker. Buffer and flush at ~30fps.
	const assistantBufferRef = useRef('');
	// Text already moved into the transcript mid-turn (see commitAssistantText).
	const committedAssistantRef = useRef('');
	const pendingAssistantDeltaRef = useRef('');
	const assistantFlushTimerRef = useRef<NodeJS.Timeout | null>(null);
	const thinkingBufferRef = useRef('');
	const thinkingStartedAtRef = useRef<number | null>(null);
	const pendingThinkingDeltaRef = useRef('');
	const thinkingFlushTimerRef = useRef<NodeJS.Timeout | null>(null);
	const pendingTranscriptItemsRef = useRef<TranscriptItem[]>([]);
	const transcriptFlushTimerRef = useRef<NodeJS.Timeout | null>(null);
	const toolStartedAtRef = useRef(new Map<string, number>());
	const toolStartedByNameRef = useRef(new Map<string, number[]>());

	const flushAssistantDelta = (): void => {
		const pending = pendingAssistantDeltaRef.current;
		if (!pending) {
			return;
		}
		pendingAssistantDeltaRef.current = '';
		assistantBufferRef.current += pending;
		startTransition(() => {
			setAssistantBuffer(assistantBufferRef.current);
		});
	};

	const flushTranscriptItems = (): void => {
		const pending = pendingTranscriptItemsRef.current;
		if (pending.length === 0) {
			return;
		}
		pendingTranscriptItemsRef.current = [];
		startTransition(() => {
			setTranscript((items) => [...items, ...pending]);
		});
	};

	const queueTranscriptItem = (item: TranscriptItem): void => {
		pendingTranscriptItemsRef.current.push(item);
		if (!transcriptFlushTimerRef.current) {
			transcriptFlushTimerRef.current = setTimeout(() => {
				transcriptFlushTimerRef.current = null;
				flushTranscriptItems();
			}, TRANSCRIPT_EVENT_FLUSH_MS);
		}
	};

	const flushThinkingDelta = (): void => {
		if (thinkingFlushTimerRef.current) {
			clearTimeout(thinkingFlushTimerRef.current);
			thinkingFlushTimerRef.current = null;
		}
		const pending = pendingThinkingDeltaRef.current;
		if (!pending) {
			return;
		}
		pendingThinkingDeltaRef.current = '';
		thinkingBufferRef.current += pending;
		startTransition(() => {
			setThinkingBuffer(thinkingBufferRef.current);
		});
	};

	/**
	 * Collapse the live thinking block into one transcript row.
	 *
	 * Mirrors Claude Code: the reasoning is visible while it streams, then
	 * collapses to ``✻ Thought for Ns``; the full text stays in the ctrl+o
	 * transcript.
	 */
	const commitThinking = (): void => {
		flushThinkingDelta();
		const text = thinkingBufferRef.current;
		if (text.trim()) {
			const startedAt = thinkingStartedAtRef.current;
			queueTranscriptItem({
				role: 'thinking',
				text,
				duration_ms: startedAt == null ? undefined : Date.now() - startedAt,
			});
			flushTranscriptItems();
		}
		thinkingBufferRef.current = '';
		thinkingStartedAtRef.current = null;
		if (thinkingFlushTimerRef.current) {
			clearTimeout(thinkingFlushTimerRef.current);
			thinkingFlushTimerRef.current = null;
		}
		setThinkingBuffer('');
	};

	const discardThinking = (): void => {
		pendingThinkingDeltaRef.current = '';
		thinkingBufferRef.current = '';
		thinkingStartedAtRef.current = null;
		if (thinkingFlushTimerRef.current) {
			clearTimeout(thinkingFlushTimerRef.current);
			thinkingFlushTimerRef.current = null;
		}
		setThinkingBuffer('');
	};

	// Out-of-band items (busy-safe command output, a queued follow-up echo) are
	// appended to the transcript, which renders *above* the live streaming
	// bubble. Committing the text streamed so far keeps the reading order
	// truthful instead of showing the item before older generated text.
	const commitAssistantText = (): void => {
		flushAssistantDelta();
		const text = assistantBufferRef.current;
		if (!text.trim()) {
			return;
		}
		committedAssistantRef.current += text;
		assistantBufferRef.current = '';
		if (assistantFlushTimerRef.current) {
			clearTimeout(assistantFlushTimerRef.current);
			assistantFlushTimerRef.current = null;
		}
		setAssistantBuffer('');
		queueTranscriptItem({role: 'assistant', text});
		flushTranscriptItems();
	};

	const clearAssistantDelta = (): void => {
		pendingAssistantDeltaRef.current = '';
		assistantBufferRef.current = '';
		committedAssistantRef.current = '';
		if (assistantFlushTimerRef.current) {
			clearTimeout(assistantFlushTimerRef.current);
			assistantFlushTimerRef.current = null;
		}
		setAssistantBuffer('');
	};

	const clearPendingTranscriptItems = (): void => {
		pendingTranscriptItemsRef.current = [];
		if (transcriptFlushTimerRef.current) {
			clearTimeout(transcriptFlushTimerRef.current);
			transcriptFlushTimerRef.current = null;
		}
	};

	const sendRequest = (payload: Record<string, unknown>): void => {
		const child = childRef.current;
		if (!child || child.stdin.destroyed) {
			return;
		}
		child.stdin.write(JSON.stringify(payload) + '\n');
	};

	useEffect(() => {
		const [command, ...args] = config.backend_command;
		const useDetachedGroup = process.platform !== 'win32';
		const child = spawn(command, args, {
			stdio: ['pipe', 'pipe', 'inherit'],
			env: process.env,
			// On Windows, a detached child gets its own console window and can
			// flash open/closed. Keep detached groups for POSIX only.
			detached: useDetachedGroup,
			windowsHide: true,
		});
		childRef.current = child;

		const reader = readline.createInterface({input: child.stdout});
		reader.on('line', (line) => {
			if (!line.startsWith(PROTOCOL_PREFIX)) {
				queueTranscriptItem({role: 'log', text: line});
				return;
			}
			const event = JSON.parse(line.slice(PROTOCOL_PREFIX.length)) as BackendEvent;
			handleEvent(event);
		});

		child.on('exit', (code) => {
			flushTranscriptItems();
			queueTranscriptItem({role: 'system', text: `backend exited with code ${code ?? 0}`});
			process.exitCode = code ?? 0;
			onExit(code);
		});

		// Ensure child processes are killed on parent exit (prevents stale processes)
		const killChild = (): void => {
			if (!child.killed) {
				// Kill the whole process group on POSIX. On Windows, terminate the
				// direct child to avoid relying on negative PIDs.
				try {
					if (useDetachedGroup && child.pid) {
						process.kill(-child.pid, 'SIGTERM');
					} else {
						child.kill('SIGTERM');
					}
				} catch {
					child.kill('SIGTERM');
				}
			}
			if (assistantFlushTimerRef.current) {
				clearTimeout(assistantFlushTimerRef.current);
				assistantFlushTimerRef.current = null;
			}
			if (thinkingFlushTimerRef.current) {
				clearTimeout(thinkingFlushTimerRef.current);
				thinkingFlushTimerRef.current = null;
			}
			pendingThinkingDeltaRef.current = '';
			thinkingBufferRef.current = '';
			thinkingStartedAtRef.current = null;
			clearPendingTranscriptItems();
		};
		process.on('exit', killChild);
		process.on('SIGINT', killChild);
		process.on('SIGTERM', killChild);

		return () => {
			reader.close();
			killChild();
			process.removeListener('exit', killChild);
			process.removeListener('SIGINT', killChild);
			process.removeListener('SIGTERM', killChild);
		};
	}, []);

	const handleEvent = (event: BackendEvent): void => {
		if (event.type === 'ready') {
			setReady(true);
			const statusSnapshot = stableStringify(event.state ?? {});
			lastStatusSnapshotRef.current = statusSnapshot;
			const nextStatus = event.state ?? {};
			statusRef.current = nextStatus;
			// Status is small and changes at most a few times per turn; painting
			// it urgently keeps the ctx meter in step with the backend instead of
			// letting a busy streaming render starve a low-priority transition.
			setStatus(nextStatus);
			const tasksSnapshot = stableStringify(event.tasks ?? []);
			lastTasksSnapshotRef.current = tasksSnapshot;
			startTransition(() => {
				setTasks(event.tasks ?? []);
			});
			setCommands(event.commands ?? []);
			setBusySafeCommands(event.busy_safe_commands ?? []);
			setCommandDescriptions(event.command_descriptions ?? {});
			const mcpSnapshot = stableStringify(event.mcp_servers ?? []);
			lastMcpSnapshotRef.current = mcpSnapshot;
			startTransition(() => {
				setMcpServers(event.mcp_servers ?? []);
			});
			const bridgeSnapshot = stableStringify(event.bridge_sessions ?? []);
			lastBridgeSnapshotRef.current = bridgeSnapshot;
			startTransition(() => {
				setBridgeSessions(event.bridge_sessions ?? []);
			});
			if (config.initial_prompt && !sentInitialPrompt.current) {
				sentInitialPrompt.current = true;
				sendRequest({type: 'submit_line', line: config.initial_prompt});
				setBusy(true);
			}
			return;
		}
		if (event.type === 'state_snapshot') {
			const statusSnapshot = stableStringify(event.state ?? {});
			if (statusSnapshot !== lastStatusSnapshotRef.current) {
				lastStatusSnapshotRef.current = statusSnapshot;
				const nextStatus = event.state ?? {};
				statusRef.current = nextStatus;
				setStatus(nextStatus);
			}
			const mcpSnapshot = stableStringify(event.mcp_servers ?? []);
			if (mcpSnapshot !== lastMcpSnapshotRef.current) {
				lastMcpSnapshotRef.current = mcpSnapshot;
				startTransition(() => {
					setMcpServers(event.mcp_servers ?? []);
				});
			}
			const bridgeSnapshot = stableStringify(event.bridge_sessions ?? []);
			if (bridgeSnapshot !== lastBridgeSnapshotRef.current) {
				lastBridgeSnapshotRef.current = bridgeSnapshot;
				startTransition(() => {
					setBridgeSessions(event.bridge_sessions ?? []);
				});
			}
			return;
		}
		if (event.type === 'tasks_snapshot') {
			const tasksSnapshot = stableStringify(event.tasks ?? []);
			if (tasksSnapshot !== lastTasksSnapshotRef.current) {
				lastTasksSnapshotRef.current = tasksSnapshot;
				startTransition(() => {
					setTasks(event.tasks ?? []);
				});
			}
			return;
		}
		if (event.type === 'transcript_item' && event.item) {
			commitAssistantText();
			queueTranscriptItem(event.item as TranscriptItem);
			return;
		}
		if (event.type === 'status') {
			const message = event.message?.trim();
			if (!message) {
				return;
			}
			queueTranscriptItem({role: 'status', text: message});
			if (busy) {
				setBusyLabel(message);
			}
			return;
		}
		if (event.type === 'compact_progress') {
			const phase = String(event.compact_phase ?? '');
			const trigger = String(event.compact_trigger ?? '');
			const attempt = event.attempt != null ? Number(event.attempt) : undefined;
			if (phase === 'hooks_start') {
				setBusyLabel(
					trigger === 'reactive'
						? 'Preparing retry compaction…'
						: 'Preparing conversation compaction…',
				);
			} else if (phase === 'context_collapse_start') {
				setBusyLabel('Collapsing oversized context…');
			} else if (phase === 'context_collapse_end') {
				setBusyLabel('Context collapse complete…');
			} else if (phase === 'session_memory_start') {
				setBusyLabel('Condensing earlier conversation…');
			} else if (phase === 'compact_start') {
				setBusyLabel(
					trigger === 'reactive'
						? 'Context is too large. Compacting and retrying…'
						: 'Compacting conversation memory…',
				);
			} else if (phase === 'compact_retry') {
				setBusyLabel(attempt ? `Retrying compaction (${attempt})…` : 'Retrying compaction…');
			} else if (phase === 'compact_end') {
				setBusyLabel('Compaction complete. Continuing…');
			} else if (phase === 'compact_failed') {
				setBusyLabel('Compaction failed. Continuing without it…');
			}
			if (event.message) {
				queueTranscriptItem({role: 'status', text: event.message!});
			}
			return;
		}
		if (event.type === 'thinking_delta') {
			const delta = event.message ?? '';
			if (!delta) {
				return;
			}
			if (thinkingStartedAtRef.current == null) {
				thinkingStartedAtRef.current = Date.now();
			}
			pendingThinkingDeltaRef.current += delta;
			if (pendingThinkingDeltaRef.current.length >= THINKING_DELTA_FLUSH_CHARS) {
				flushThinkingDelta();
				return;
			}
			if (!thinkingFlushTimerRef.current) {
				thinkingFlushTimerRef.current = setTimeout(() => {
					thinkingFlushTimerRef.current = null;
					flushThinkingDelta();
				}, THINKING_DELTA_FLUSH_MS);
			}
			return;
		}
		if (event.type === 'assistant_delta') {
			// Thinking is done once the answer starts; collapse it to one row.
			commitThinking();
			const delta = event.message ?? '';
			if (!delta) {
				return;
			}
			const isCodexStyle = String(statusRef.current.output_style ?? 'default') === 'codex';
			if (isCodexStyle) {
				// Keep collecting text for assistant_complete fallback, but avoid
				// token-level rerenders in compact codex mode.
				assistantBufferRef.current += delta;
				return;
			}
			pendingAssistantDeltaRef.current += delta;
			if (pendingAssistantDeltaRef.current.length >= ASSISTANT_DELTA_FLUSH_CHARS) {
				flushAssistantDelta();
				return;
			}
			if (!assistantFlushTimerRef.current) {
				assistantFlushTimerRef.current = setTimeout(() => {
					assistantFlushTimerRef.current = null;
					flushAssistantDelta();
				}, ASSISTANT_DELTA_FLUSH_MS);
			}
			return;
		}
		if (event.type === 'assistant_complete') {
			if (assistantFlushTimerRef.current) {
				clearTimeout(assistantFlushTimerRef.current);
				assistantFlushTimerRef.current = null;
			}
			commitThinking();
			flushTranscriptItems();
			const isCodexStyle = String(statusRef.current.output_style ?? 'default') === 'codex';
			if (isCodexStyle) {
				if (pendingAssistantDeltaRef.current) {
					assistantBufferRef.current += pendingAssistantDeltaRef.current;
					pendingAssistantDeltaRef.current = '';
				}
			} else {
				flushAssistantDelta();
			}
			const fullText = event.message ?? assistantBufferRef.current;
			const committed = committedAssistantRef.current;
			// A mid-turn commit already put a prefix of this message on screen,
			// so only the remainder belongs in this transcript item.
			const text =
				committed && fullText.startsWith(committed)
					? fullText.slice(committed.length)
					: fullText;
			if (text.trim()) {
				startTransition(() => {
					setTranscript((items) => [...items, {role: 'assistant', text}]);
				});
			}
			clearAssistantDelta();
			// Do NOT reset busy here: tool calls may follow this event.
			// busy is reset by line_complete (the true end-of-turn signal).
			setBusyLabel(undefined);
			return;
		}
		if (event.type === 'tool_output_delta') {
			const toolUseId = event.tool_use_id;
			const delta = event.message ?? '';
			if (!toolUseId || !delta) {
				return;
			}
			setTranscript((items) => {
				const index = items.findIndex(
					(item) => item.role === 'tool' && item.tool_use_id === toolUseId,
				);
				if (index < 0) {
					return items;
				}
				const previous = items[index]?.output ?? '';
				const next = items.slice();
				// Only the tail matters for the running preview; cap the buffer
				// so a chatty command cannot grow the transcript without bound.
				next[index] = {...items[index], output: `${previous}${delta}`.slice(-8_000)};
				return next;
			});
			return;
		}
		if (event.type === 'tool_completed') {
			// Bumped so the UI can flush queued prompts at a natural boundary
			// instead of waiting for the whole turn to finish.
			setToolCompletions((count) => count + 1);
		}
		if (event.type === 'line_complete') {
			// Final end-of-turn: clear everything, stop spinner.
			commitThinking();
			flushTranscriptItems();
			clearAssistantDelta();
			setBusy(false);
			setBusyLabel(undefined);
			if (process.env.OPENHARNESS_TUI_BELL !== '0') {
				process.stdout.write('\u0007');
			}
			return;
		}
		if (event.type === 'file_search_results') {
			setFileMatches(event.files ?? []);
			return;
		}
		if ((event.type === 'tool_started' || event.type === 'tool_completed') && event.item) {
			commitThinking();
			const toolName = event.item.tool_name ?? event.tool_name ?? 'tool';
			const toolUseId = event.item.tool_use_id ?? event.tool_use_id ?? null;
			let startedAt: number | undefined;
			if (event.type === 'tool_started') {
				const now = Date.now();
				if (toolUseId) {
					toolStartedAtRef.current.set(toolUseId, now);
				} else {
					const queue = toolStartedByNameRef.current.get(toolName) ?? [];
					queue.push(now);
					toolStartedByNameRef.current.set(toolName, queue);
				}
				setBusy(true);
				setBusyLabel(`Running ${toolName}...`);
			} else {
				if (toolUseId) {
					startedAt = toolStartedAtRef.current.get(toolUseId);
					toolStartedAtRef.current.delete(toolUseId);
				} else {
					const queue = toolStartedByNameRef.current.get(toolName);
					startedAt = queue?.shift();
					if (queue && queue.length === 0) {
						toolStartedByNameRef.current.delete(toolName);
					}
				}
				setBusyLabel('Processing...');
			}
			const enrichedItem: TranscriptItem = {
				...event.item,
				tool_name: toolName === 'tool' ? undefined : toolName,
				tool_input: event.item.tool_input ?? undefined,
				is_error: event.item.is_error ?? event.is_error ?? undefined,
				duration_ms:
					event.type === 'tool_completed' && startedAt != null
						? Date.now() - startedAt
						: event.item.duration_ms,
			};
			if (event.type === 'tool_started') {
				// Install the row immediately: live output deltas that follow it
				// must find the tool by id.  Queued rows are folded into the same
				// update so the order is unchanged.
				const pending = pendingTranscriptItemsRef.current;
				pendingTranscriptItemsRef.current = [];
				setTranscript((items) => [...items, ...pending, enrichedItem]);
			} else {
				queueTranscriptItem(enrichedItem);
			}
			return;
		}
		if (event.type === 'clear_transcript') {
			discardThinking();
			flushTranscriptItems();
			clearPendingTranscriptItems();
			setTranscript([]);
			clearAssistantDelta();
			setBusyLabel(undefined);
			return;
		}
		if (event.type === 'select_request') {
			const m = event.modal ?? {};
			setSelectRequest({
				title: String(m.title ?? 'Select'),
				command: String(m.command ?? ''),
				options: event.select_options ?? [],
				searchable: m.searchable === true,
			});
			return;
		}
		if (event.type === 'modal_request') {
			setModal(event.modal ?? null);
			return;
		}
		if (event.type === 'error') {
			commitThinking();
			flushTranscriptItems();
			queueTranscriptItem({role: 'system', text: `error: ${event.message ?? 'unknown error'}`});
			clearAssistantDelta();
			setBusy(false);
			setBusyLabel(undefined);
			return;
		}
		if (event.type === 'todo_update') {
			if (event.todo_markdown != null) {
				startTransition(() => {
					setTodoMarkdown(event.todo_markdown);
				});
			}
			return;
		}
		if (event.type === 'swarm_status') {
			if (event.swarm_teammates != null) {
				startTransition(() => {
					setSwarmTeammates(event.swarm_teammates);
				});
			}
			if (event.swarm_notifications != null) {
				startTransition(() => {
					setSwarmNotifications((prev) => [...prev, ...event.swarm_notifications!].slice(-20));
				});
			}
			return;
		}
		if (event.type === 'plan_mode_change') {
			if (event.plan_mode != null) {
				setStatus((s) => {
					const next = {...s, permission_mode: event.plan_mode};
					statusRef.current = next;
					return next;
				});
			}
			return;
		}
		if (event.type === 'shutdown') {
			onExit(0);
		}
	};

	return useMemo(
		() => ({
			transcript,
			assistantBuffer,
			thinkingBuffer,
			status,
			tasks,
			commands,
			busySafeCommands,
			commandDescriptions,
			fileMatches,
			mcpServers,
			bridgeSessions,
			modal,
			selectRequest,
			busy,
			busyLabel,
			ready,
			todoMarkdown,
			toolCompletions,
			swarmTeammates,
			swarmNotifications,
			setModal,
			setSelectRequest,
			setBusy,
			setBusyLabel,
			sendRequest,
			requestFiles: (query: string) =>
				sendRequest({type: 'search_files', query}),
		}),
		[assistantBuffer, bridgeSessions, busy, busyLabel, busySafeCommands, commandDescriptions, commands, fileMatches, mcpServers, modal, ready, selectRequest, status, swarmNotifications, swarmTeammates, tasks, thinkingBuffer, todoMarkdown, toolCompletions, transcript]
	);
}
