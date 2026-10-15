import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { detectReport } from "./api";
import { App } from "./App";
import { Console } from "./Console";
import "./styles.css";

const root = document.getElementById("root");
if (root) {
  // A single-file HTML report carries its own data; otherwise talk to `replex console`.
  const report = detectReport(document);
  createRoot(root).render(<StrictMode>{report ? <App source={report} /> : <Console />}</StrictMode>);
}
