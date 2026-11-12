import { createRoot } from "react-dom/client";
import { App } from "./App.tsx";
import "./styles.css";
import "./exports.js";

createRoot(document.getElementById("root")!).render(<App />);
