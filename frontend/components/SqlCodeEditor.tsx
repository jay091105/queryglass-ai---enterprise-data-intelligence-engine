"use client";

import CodeMirror from "@uiw/react-codemirror";
import { sql } from "@codemirror/lang-sql";
import { EditorView } from "@codemirror/view";
import { HighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { tags } from "@lezer/highlight";

const editorTheme = EditorView.theme({
  "&": { color: "var(--text)", backgroundColor: "var(--background)", fontSize: "13px", fontFamily: "var(--mono)" },
  ".cm-scroller": { backgroundColor: "var(--background)" },
  ".cm-content": { backgroundColor: "var(--background)", fontFamily: "var(--mono)", padding: "12px 0", caretColor: "var(--text)" },
  ".cm-cursor": { borderLeftColor: "var(--text)" },
  ".cm-placeholder": { color: "var(--muted)", fontFamily: "var(--mono)" },
  ".cm-gutters": { backgroundColor: "var(--background)", color: "var(--muted)", borderRight: "1px solid var(--border)" },
  ".cm-activeLine": { backgroundColor: "color-mix(in srgb, var(--accent) 10%, var(--background))" },
  ".cm-activeLineGutter": { color: "var(--text)", backgroundColor: "color-mix(in srgb, var(--accent) 10%, var(--background))" },
  ".cm-focused": { outline: "none" },
  "&.cm-focused .cm-selectionBackground, .cm-selectionBackground": { backgroundColor: "color-mix(in srgb, var(--accent) 32%, transparent) !important" },
});

const editorHighlight = HighlightStyle.define([
  { tag: tags.keyword, color: "var(--sql-keyword)" },
  { tag: tags.operator, color: "var(--sql-operator)" },
  { tag: tags.string, color: "var(--sql-string)" },
  { tag: tags.number, color: "var(--sql-number)" },
  { tag: tags.typeName, color: "var(--sql-type)" },
  { tag: tags.function(tags.variableName), color: "var(--sql-function)" },
  { tag: tags.comment, color: "var(--sql-comment)", fontStyle: "italic" },
]);

export function SqlCodeEditor({
  value,
  onChange,
}: {
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <CodeMirror
      value={value}
      height="228px"
      theme="none"
      extensions={[sql(), editorTheme, syntaxHighlighting(editorHighlight)]}
      onChange={onChange}
      basicSetup={{ lineNumbers: true, foldGutter: true, highlightActiveLine: true, autocompletion: true }}
      aria-label="Editable SQL query"
    />
  );
}
