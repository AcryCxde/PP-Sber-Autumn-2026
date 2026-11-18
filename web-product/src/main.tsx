import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { adminRedirect } from "./adminRedirect.ts";
import { App } from "./App.tsx";
import { AuthGate } from "./auth.tsx";
import "./style.css";

function Root() {
  const [redirect, setRedirect] = useState(() => adminRedirect(window.location.hash));
  useEffect(() => {
    const update = () => setRedirect(adminRedirect(window.location.hash));
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  useEffect(() => {
    if (redirect) window.location.replace(redirect);
  }, [redirect]);
  if (redirect) return <p>Переход в администрирование…</p>;
  return (
    <AuthGate>
      {(user, logout) => <App key={user.id} user={user} onLogout={logout} />}
    </AuthGate>
  );
}

createRoot(document.getElementById("root")!).render(<Root />);
