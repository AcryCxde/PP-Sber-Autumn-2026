import { createRoot } from "react-dom/client";
import { App } from "./App.tsx";
import { AuthGate } from "./auth.tsx";
import "./style.css";
createRoot(document.getElementById("root")!).render(
  // key: another account gets a fresh workspace with its own drafts.
  <AuthGate>{(user, logout) => <App key={user.id} user={user} onLogout={logout} />}</AuthGate>,
);
