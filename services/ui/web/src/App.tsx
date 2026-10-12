import type { MouseEvent } from "react";
import { HashRouter, Link, NavLink, Route, Routes } from "react-router";
import { ArticlePage } from "./pages/ArticlePage";
import { ProcessesPage } from "./pages/ProcessesPage";
import { ProcessPage } from "./pages/ProcessPage";
import { UploadPage } from "./pages/UploadPage";

function NotFound() {
  return (
    <section>
      <h1>Page not found</h1>
      <p>
        <Link to="/">Go to Upload</Link>
      </p>
    </section>
  );
}

export function App() {
  return (
    <HashRouter>
      <a className="skip-link" href="#main" onClick={skipToMain}>
        Skip to content
      </a>
      <header className="topbar">
        <div className="topbar-inner">
          <Link to="/" className="brand">
            entingestion
          </Link>
          <nav aria-label="Main">
            <NavLink to="/" end>
              Upload
            </NavLink>
            <NavLink to="/processes">Processes</NavLink>
          </nav>
        </div>
      </header>
      <main id="main" className="container" tabIndex={-1}>
        <Routes>
          <Route path="/" element={<UploadPage />} />
          <Route path="/processes" element={<ProcessesPage />} />
          <Route path="/processes/:id" element={<ProcessPage />} />
          <Route path="/articles/:id" element={<ArticlePage />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </main>
    </HashRouter>
  );
}

// With a hash router, "#main" would be read as a route; move focus instead.
function skipToMain(ev: MouseEvent<HTMLAnchorElement>) {
  ev.preventDefault();
  document.getElementById("main")?.focus();
}
