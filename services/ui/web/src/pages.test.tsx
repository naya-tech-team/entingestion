// Render smoke test: every page renders its first frame (no effects run on the server, so no
// API calls are made).
import type { ReactElement } from "react";
import { renderToString } from "react-dom/server";
import { MemoryRouter, Route, Routes } from "react-router";
import { describe, expect, it } from "vitest";
import { ConfirmAction } from "./components/ConfirmAction";
import { FileRow } from "./components/FileRow";
import { ArticlePage } from "./pages/ArticlePage";
import { DlqPage } from "./pages/ops/DlqPage";
import { FailedPage } from "./pages/ops/FailedPage";
import { OpsLayout } from "./pages/ops/OpsLayout";
import { OverviewPage } from "./pages/ops/OverviewPage";
import { SearchPage } from "./pages/ops/SearchPage";
import { TablesPage } from "./pages/ops/TablesPage";
import { ProcessesPage } from "./pages/ProcessesPage";
import { ProcessPage } from "./pages/ProcessPage";
import { UploadPage } from "./pages/UploadPage";

function renderOps(url: string, path: string, element: ReactElement): string {
  return renderToString(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/ops" element={<OpsLayout />}>
          {path === "" ? <Route index element={element} /> : <Route path={path} element={element} />}
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

function render(url: string, path: string, element: ReactElement): string {
  return renderToString(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path={path} element={element} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("pages", () => {
  it("Upload", () => {
    const html = render("/", "/", <UploadPage />);
    expect(html).toContain("Upload documents");
    expect(html).toContain('role="button"'); // the keyboard-usable drop zone
    expect(html).toContain('accept=".html,.htm,.txt,.md,.markdown,.pdf,.docx"');
  });

  it("Processes", () => {
    expect(render("/processes?source=bruno", "/processes", <ProcessesPage />)).toContain("Processes");
  });

  it("Process", () => {
    expect(render("/processes/p-123", "/processes/:id", <ProcessPage />)).toContain("p-123");
  });

  it("Article", () => {
    expect(render("/articles/a-9", "/articles/:id", <ArticlePage />)).toContain("a-9");
  });

  it("Ops overview", () => {
    const html = renderOps("/ops", "", <OverviewPage />);
    expect(html).toContain("Operations");
    expect(html).toContain('aria-label="Operations"'); // the sub-nav
    expect(html).toContain("Failed &amp; stuck");
  });

  it("Ops failed & stuck", () => {
    const html = renderOps("/ops/articles?status=FAILED_RETRYABLE", "articles", <FailedPage />);
    expect(html).toContain("Failed &amp; stuck articles");
    expect(html).toContain("Re-run selected");
    expect(html).toMatch(/<option value="FAILED_RETRYABLE" selected="">/);
  });

  it("Ops DLQ", () => {
    const html = renderOps("/ops/dlq", "dlq", <DlqPage />);
    expect(html).toContain("Preview redrive");
    expect(html).toContain('value="50"');
  });

  it("Ops search", () => {
    const html = renderOps("/ops/search", "search", <SearchPage />);
    expect(html).toContain("<form");
    expect(html).toContain('id="search-query"');
  });

  it("Ops tables", () => {
    expect(renderOps("/ops/tables", "tables", <TablesPage />)).toContain("Vector tables");
  });

  it("FileRow shows a queued file's progress, or its validation error", () => {
    const base = { id: "f1", title: "", externalId: "", error: null, state: null };
    const file = new File(["x"], "a.pdf", { type: "application/pdf" });
    const row = (entry: Parameters<typeof FileRow>[0]["entry"], locked = false) =>
      renderToString(
        <MemoryRouter>
          <FileRow entry={entry} locked={locked} onChange={() => undefined} onRemove={() => undefined} />
        </MemoryRouter>,
      );
    expect(row({ ...base, file })).toContain("Ready");
    expect(row({ ...base, file })).toContain("Remove");
    const done = row({ ...base, file, state: { phase: "done", articleId: "a-1", uploadId: "u-1" } }, true);
    expect(done).toContain('href="/articles/a-1"');
    expect(done).not.toContain("Remove");
    expect(row({ ...base, file, error: "The file is empty" })).toContain("The file is empty");
  });

  it("ConfirmAction starts closed, with no dialog", () => {
    const html = renderToString(<ConfirmAction label="Re-run" run={() => Promise.reject(new Error("not called"))} />);
    expect(html).toContain("Re-run");
    expect(html).not.toContain('role="dialog"');
  });
});
