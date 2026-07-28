"use client";

import { useEffect } from "react";

export default function LegacySecretCleanup() {
  useEffect(() => {
    // Versions before the secure multi-tenant migration persisted raw keys in
    // this entry. Remove the entire object once; never inspect or migrate it.
    localStorage.removeItem("polybot_settings");
  }, []);

  return null;
}
