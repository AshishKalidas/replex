// Word-level diff for changed text (final answers, replies) using LCS on tokens.

export type Piece = { op: "same" | "add" | "del"; text: string };

const MAX_TOKENS = 800;

export function tokenize(text: string): string[] {
  return text.match(/\s+|[\p{L}\p{N}_]+|[^\s\p{L}\p{N}_]/gu) ?? [];
}

export function wordDiff(before: string, after: string): Piece[] {
  const a = tokenize(before);
  const b = tokenize(after);
  if (a.length > MAX_TOKENS || b.length > MAX_TOKENS) {
    return [
      ...(before ? [{ op: "del" as const, text: before }] : []),
      ...(after ? [{ op: "add" as const, text: after }] : []),
    ];
  }
  const n = a.length;
  const m = b.length;
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    const row = dp[i]!;
    const next = dp[i + 1]!;
    for (let j = m - 1; j >= 0; j--) {
      row[j] = a[i] === b[j] ? next[j + 1]! + 1 : Math.max(next[j]!, row[j + 1]!);
    }
  }
  const out: Piece[] = [];
  const push = (op: Piece["op"], text: string) => {
    const last = out[out.length - 1];
    if (last && last.op === op) last.text += text;
    else out.push({ op, text });
  };
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      push("same", a[i]!);
      i++;
      j++;
    } else if (dp[i + 1]![j]! >= dp[i]![j + 1]!) {
      push("del", a[i]!);
      i++;
    } else {
      push("add", b[j]!);
      j++;
    }
  }
  while (i < n) push("del", a[i++]!);
  while (j < m) push("add", b[j++]!);
  return out;
}

/**
 * A word diff when the texts are mostly alike; otherwise a plain before/after replacement,
 * which reads better than a confetti of tiny edits.
 */
export function readableDiff(before: string, after: string, minShared = 0.5): Piece[] {
  const pieces = wordDiff(before, after);
  const words = (s: string) => s.replace(/\s+/g, "").length;
  const shared = pieces.filter((p) => p.op === "same").reduce((n, p) => n + words(p.text), 0);
  const longest = Math.max(words(before), words(after), 1);
  if (shared / longest >= minShared) return pieces;
  return [
    ...(before ? [{ op: "del" as const, text: before }] : []),
    ...(after ? [{ op: "add" as const, text: (before ? "\n" : "") + after }] : []),
  ];
}
