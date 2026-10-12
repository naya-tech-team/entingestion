// Render smoke test: every page renders its first frame (no effects run on the server, so no
// API calls are made).
import type { ReactElement } from "react";
import { renderToString } from "react-dom/server";
import { MemoryRouter, Route, Routes } from "react-router";
import { describe, expect, it } from "vitest";
import { ArticlePage } from "./pages/ArticlePage";
import { ProcessesPage } from "./pages/ProcessesPage";
import { ProcessPage } from "./pages/ProcessPage";
import { UploadPage } from "./pages/UploadPage";

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
});
