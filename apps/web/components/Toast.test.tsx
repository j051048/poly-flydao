import { act, render, renderHook, screen } from "@testing-library/react";
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import ToastViewport, { useToasts } from "./Toast";

afterEach(() => {
  vi.useRealTimers();
});

describe("useToasts", () => {
  it("keeps the three most recent messages and auto-dismisses them", () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useToasts(1_000));

    act(() => {
      result.current.push("first");
      result.current.push("second", "success");
      result.current.push("third", "error");
      result.current.push("fourth");
    });

    expect(result.current.toasts.map((toast) => toast.text)).toEqual([
      "second",
      "third",
      "fourth",
    ]);

    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    expect(result.current.toasts).toHaveLength(0);
  });

  it("ignores empty messages and dismisses on demand", () => {
    const { result } = renderHook(() => useToasts());

    act(() => result.current.push(""));
    expect(result.current.toasts).toHaveLength(0);

    act(() => result.current.push("something went wrong", "error"));
    const id = result.current.toasts[0].id;
    act(() => result.current.dismiss(id));
    expect(result.current.toasts).toHaveLength(0);
  });
});

describe("ToastViewport", () => {
  it("renders nothing without messages and exposes a live region otherwise", () => {
    const { rerender } = render(
      <ToastViewport toasts={[]} onDismiss={() => undefined} />,
    );
    expect(screen.queryByRole("status")).toBeNull();

    rerender(
      <ToastViewport
        toasts={[{ id: 1, tone: "success", text: "周期已创建" }]}
        onDismiss={() => undefined}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("周期已创建");
  });
});
