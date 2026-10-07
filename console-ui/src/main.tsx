import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { detectSource } from "./api";
import { App } from "./App";
import "./styles.css";

const root = document.getElementById("root");
if (root) {
  createRoot(root).render(
    <StrictMode>
      <App source={detectSource(document)} />
    </StrictMode>,
  );
}
