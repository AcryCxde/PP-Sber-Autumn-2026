import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { AdminPanel } from "./AdminPanel.tsx";
import { App } from "./App.tsx";
import { AuthGate } from "./auth.tsx";
import "./admin.css";
import "./style.css";

// #admin: the build uses relative assets (base "./"), so the panel lives in the hash, not a path.
function useAdminHash() {
  const [admin, setAdmin] = useState(() => window.location.hash === "#admin");
  useEffect(() => {
    const update = () => setAdmin(window.location.hash === "#admin");
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  return admin;
}

function Root() {
  const admin = useAdminHash();
  return (
    <AuthGate>
      {(user, logout) =>
        admin && user.role === "admin" ? (
          <AdminPanel user={user} onLogout={logout} />
        ) : (
          // key: another account gets a fresh workspace with its own drafts.
          <App key={user.id} user={user} onLogout={logout} />
        )
      }
    </AuthGate>
  );
}

createRoot(document.getElementById("root")!).render(<Root />);
