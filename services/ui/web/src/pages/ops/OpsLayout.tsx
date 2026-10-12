import { NavLink, Outlet } from "react-router";

/** The Operations area: a sub-navigation over its pages. */
export function OpsLayout() {
  return (
    <>
      <nav aria-label="Operations" className="subnav">
        <NavLink to="/ops" end>
          Overview
        </NavLink>
        <NavLink to="/ops/articles?status=FAILED_PERMANENT">Failed &amp; stuck</NavLink>
        <NavLink to="/ops/dlq">DLQ</NavLink>
        <NavLink to="/ops/search">Search</NavLink>
        <NavLink to="/ops/tables">Tables</NavLink>
      </nav>
      <Outlet />
    </>
  );
}
