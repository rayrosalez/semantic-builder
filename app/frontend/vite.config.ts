import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Local dev: `uvicorn app:app --port 8000` in app/, then `npm run dev` here.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://localhost:8000" } },
});
