import { render } from "@testing-library/react";
import React from "react";
import { describe, expect, it, vi } from "vitest";

import LegacySecretCleanup from "./LegacySecretCleanup";

describe("LegacySecretCleanup", () => {
  it("removes the pre-migration settings object without reading it", () => {
    const removeItem = vi.fn();
    const getItem = vi.fn();
    vi.stubGlobal("localStorage", { removeItem, getItem });

    render(<LegacySecretCleanup />);

    expect(removeItem).toHaveBeenCalledWith("polybot_settings");
    expect(getItem).not.toHaveBeenCalledWith("polybot_settings");
    vi.unstubAllGlobals();
  });
});
