import { For, Show, createMemo, onCleanup, onMount, type Component } from "solid-js"
import { createStore } from "solid-js/store"
import { useMutation } from "@tanstack/solid-query"
import { Button } from "@opencode-ai/ui/button"
import { showToast } from "@opencode-ai/ui/toast"
import type { QuestionAnswer, QuestionRequest } from "@opencode-ai/sdk/v2"
import { useLanguage } from "@/context/language"
import { useSDK } from "@/context/sdk"
import { makeEventListener } from "@solid-primitives/event-listener"
import { createResizeObserver } from "@solid-primitives/resize-observer"

const cache = new Map<string, { tab: number; answers: QuestionAnswer[]; custom: string[]; customOn: boolean[] }>()


export const SessionQuestionDock: Component<{ request: QuestionRequest; onSubmit: () => void }> = (props) => {
  const sdk = useSDK()
  const language = useLanguage()

  const questions = createMemo(() => props.request.questions)
  const total = createMemo(() => questions().length)

  const cached = cache.get(props.request.id)
  const [store, setStore] = createStore({
    tab: cached?.tab ?? 0,
    answers: cached?.answers ?? ([] as QuestionAnswer[]),
    custom: cached?.custom ?? ([] as string[]),
    customOn: cached?.customOn ?? ([] as boolean[]),
    editing: false,
    focus: 0,
  })

  let root: HTMLDivElement | undefined
  let customRef: HTMLButtonElement | undefined
  let optsRef: HTMLButtonElement[] = []
  let replied = false
  let focusFrame: number | undefined

  const question = createMemo(() => questions()[store.tab])
  const options = createMemo(() => question()?.options ?? [])
  const input = createMemo(() => store.custom[store.tab] ?? "")
  const on = createMemo(() => store.customOn[store.tab] === true)
  const multi = createMemo(() => question()?.multiple === true)
  const count = createMemo(() => options().length + 1)

  const summary = createMemo(() => {
    const n = Math.min(store.tab + 1, total())
    return language.t("session.question.progress", { current: n, total: total() })
  })

  const customLabel = () => language.t("ui.messagePart.option.typeOwnAnswer")
  const customPlaceholder = () => language.t("ui.question.custom.placeholder")

  const last = createMemo(() => store.tab >= total() - 1)

  const customUpdate = (value: string, selected: boolean = on()) => {
    const prev = input().trim()
    const next = value.trim()

    setStore("custom", store.tab, value)
    if (!selected) return

    if (multi()) {
      setStore("answers", store.tab, (current = []) => {
        const removed = prev ? current.filter((item) => item.trim() !== prev) : current
        if (!next) return removed
        if (removed.some((item) => item.trim() === next)) return removed
        return [...removed, next]
      })
      return
    }

    setStore("answers", store.tab, next ? [next] : [])
  }

  const measure = () => {
    if (!root) return

    const scroller = document.querySelector(".scroll-view__viewport")
    const head = scroller instanceof HTMLElement ? scroller.firstElementChild : undefined
    const top =
      head instanceof HTMLElement && head.classList.contains("sticky") ? head.getBoundingClientRect().bottom : 0
    if (!top) {
      root.style.removeProperty("--question-prompt-max-height")
      return
    }

    const dock = root.closest('[data-component="session-prompt-dock"]')
    if (!(dock instanceof HTMLElement)) return

    const dockBottom = dock.getBoundingClientRect().bottom
    const below = Math.max(0, dockBottom - root.getBoundingClientRect().bottom)
    const gap = 8
    const max = Math.max(240, Math.floor(dockBottom - top - gap - below))
    root.style.setProperty("--question-prompt-max-height", `${max}px`)
  }

  const clamp = (i: number) => Math.max(0, Math.min(count() - 1, i))

  const pickFocus = (tab: number = store.tab) => {
    const list = questions()[tab]?.options ?? []
    if (store.customOn[tab] === true) return list.length
    return Math.max(
      0,
      list.findIndex((item) => store.answers[tab]?.includes(item.label) ?? false),
    )
  }

  const focus = (i: number) => {
    const next = clamp(i)
    setStore("focus", next)
    if (store.editing) return
    if (focusFrame !== undefined) cancelAnimationFrame(focusFrame)
    focusFrame = requestAnimationFrame(() => {
      focusFrame = undefined
      const el = next === options().length ? customRef : optsRef[next]
      el?.focus()
    })
  }

  onMount(() => {
    let raf: number | undefined
    const update = () => {
      if (raf !== undefined) cancelAnimationFrame(raf)
      raf = requestAnimationFrame(() => {
        raf = undefined
        measure()
      })
    }

    update()

    makeEventListener(window, "resize", update)

    const dock = root?.closest('[data-component="session-prompt-dock"]')
    const scroller = document.querySelector(".scroll-view__viewport")
    createResizeObserver([dock, scroller], update)

    onCleanup(() => {
      if (raf !== undefined) cancelAnimationFrame(raf)
    })

    focus(pickFocus())
  })

  onCleanup(() => {
    if (focusFrame !== undefined) cancelAnimationFrame(focusFrame)
    if (replied) return
    cache.set(props.request.id, {
      tab: store.tab,
      answers: store.answers.map((a) => (a ? [...a] : [])),
      custom: store.custom.map((s) => s ?? ""),
      customOn: store.customOn.map((b) => b ?? false),
    })
  })

  const fail = (err: unknown) => {
    const message = err instanceof Error ? err.message : String(err)
    showToast({ title: language.t("common.requestFailed"), description: message })
  }

  const replyMutation = useMutation(() => ({
    mutationFn: (answers: QuestionAnswer[]) => sdk.client.question.reply({ requestID: props.request.id, answers }),
    onMutate: () => {
      props.onSubmit()
    },
    onSuccess: () => {
      replied = true
      cache.delete(props.request.id)
    },
    onError: fail,
  }))

  const rejectMutation = useMutation(() => ({
    mutationFn: () => sdk.client.question.reject({ requestID: props.request.id }),
    onMutate: () => {
      props.onSubmit()
    },
    onSuccess: () => {
      replied = true
      cache.delete(props.request.id)
    },
    onError: fail,
  }))

  const sending = createMemo(() => replyMutation.isPending || rejectMutation.isPending)

  const reply = async (answers: QuestionAnswer[]) => {
    if (sending()) return
    await replyMutation.mutateAsync(answers)
  }

  const reject = async () => {
    if (sending()) return
    await rejectMutation.mutateAsync()
  }

  const submit = () => void reply(questions().map((_, i) => store.answers[i] ?? []))

  const answered = (i: number) => {
    if ((store.answers[i]?.length ?? 0) > 0) return true
    return store.customOn[i] === true && (store.custom[i] ?? "").trim().length > 0
  }

  const picked = (answer: string) => store.answers[store.tab]?.includes(answer) ?? false

  const pick = (answer: string, custom: boolean = false) => {
    setStore("answers", store.tab, [answer])
    if (custom) setStore("custom", store.tab, answer)
    if (!custom) setStore("customOn", store.tab, false)
    setStore("editing", false)
  }

  const toggle = (answer: string) => {
    setStore("answers", store.tab, (current = []) => {
      if (current.includes(answer)) return current.filter((item) => item !== answer)
      return [...current, answer]
    })
  }

  const customToggle = () => {
    if (sending()) return
    setStore("focus", options().length)

    if (!multi()) {
      setStore("customOn", store.tab, true)
      setStore("editing", true)
      customUpdate(input(), true)
      return
    }

    const next = !on()
    setStore("customOn", store.tab, next)
    if (next) {
      setStore("editing", true)
      customUpdate(input(), true)
      return
    }

    const value = input().trim()
    if (value) setStore("answers", store.tab, (current = []) => current.filter((item) => item.trim() !== value))
    setStore("editing", false)
    focus(options().length)
  }

  const customOpen = () => {
    if (sending()) return
    setStore("focus", options().length)
    if (!on()) setStore("customOn", store.tab, true)
    setStore("editing", true)
    customUpdate(input(), true)
  }

  const move = (step: number) => {
    if (store.editing || sending()) return
    focus(store.focus + step)
  }

  const nav = (event: KeyboardEvent) => {
    if (event.defaultPrevented) return

    if (event.key === "Escape") {
      event.preventDefault()
      void reject()
      return
    }

    const mod = (event.metaKey || event.ctrlKey) && !event.altKey
    if (mod && event.key === "Enter") {
      if (event.repeat) return
      event.preventDefault()
      next()
      return
    }

    const target =
      event.target instanceof HTMLElement ? event.target.closest('[data-slot="question-options"]') : undefined
    if (store.editing) return
    if (!(target instanceof HTMLElement)) return
    if (event.altKey || event.ctrlKey || event.metaKey) return

    if (event.key === "ArrowDown" || event.key === "ArrowRight") {
      event.preventDefault()
      move(1)
      return
    }

    if (event.key === "ArrowUp" || event.key === "ArrowLeft") {
      event.preventDefault()
      move(-1)
      return
    }

    if (event.key === "Home") {
      event.preventDefault()
      focus(0)
      return
    }

    if (event.key !== "End") return
    event.preventDefault()
    focus(count() - 1)
  }

  const selectOption = (optIndex: number) => {
    if (sending()) return

    if (optIndex === options().length) {
      customOpen()
      return
    }

    const opt = options()[optIndex]
    if (!opt) return
    if (multi()) {
      setStore("editing", false)
      toggle(opt.label)
      return
    }
    pick(opt.label)
  }

  const commitCustom = () => {
    setStore("editing", false)
    customUpdate(input())
    focus(options().length)
  }

  const resizeInput = (el: HTMLTextAreaElement) => {
    el.style.height = "0px"
    el.style.height = `${el.scrollHeight}px`
  }

  const focusCustom = (el: HTMLTextAreaElement) => {
    setTimeout(() => {
      el.focus()
      resizeInput(el)
    }, 0)
  }

  const toggleCustomMark = (event: MouseEvent) => {
    event.preventDefault()
    event.stopPropagation()
    customToggle()
  }

  const next = () => {
    if (sending()) return
    if (store.editing) commitCustom()

    if (store.tab >= total() - 1) {
      submit()
      return
    }

    const tab = store.tab + 1
    setStore("tab", tab)
    setStore("editing", false)
    focus(pickFocus(tab))
  }

  const back = () => {
    if (sending()) return
    if (store.tab <= 0) return
    const tab = store.tab - 1
    setStore("tab", tab)
    setStore("editing", false)
    focus(pickFocus(tab))
  }

  const jump = (tab: number) => {
    if (sending()) return
    setStore("tab", tab)
    setStore("editing", false)
    focus(pickFocus(tab))
  }

  return (
    <div
      ref={(el) => (root = el)}
      onKeyDown={nav}
      class="mb-2 rounded-xl border border-border-weak-base bg-background-base shadow-lg overflow-hidden"
      style={{ "--question-prompt-max-height": "400px" }}
    >
      {/* Header */}
      <div class="flex items-center justify-between px-4 py-3 border-b border-border-weak-base bg-background-raised">
        <div class="flex items-center gap-2">
          <span class="text-xs font-medium text-text-weak uppercase tracking-wider">
            {language.t("ui.tool.questions")}
          </span>
          <span class="text-xs text-text-weaker">·</span>
          <span class="text-xs text-text-weak">{summary()}</span>
        </div>
        {/* Progress dots */}
        <div class="flex items-center gap-1">
          <For each={questions()}>
            {(_, i) => (
              <button
                type="button"
                disabled={sending()}
                onClick={() => jump(i())}
                aria-label={`${language.t("ui.tool.questions")} ${i() + 1}`}
                class="rounded-full transition-all duration-200 focus:outline-none"
                style={{
                  width: i() === store.tab ? "20px" : "6px",
                  height: "6px",
                  background: answered(i())
                    ? "var(--text-interactive-base)"
                    : i() === store.tab
                    ? "var(--text-base)"
                    : "var(--border-base)",
                }}
              />
            )}
          </For>
        </div>
      </div>

      {/* Body */}
      <div class="px-4 pt-4 pb-3 flex flex-col gap-3 overflow-y-auto" style={{ "max-height": "var(--question-prompt-max-height, 360px)" }}>
        {/* Question text */}
        <p class="text-sm font-medium text-text-strong leading-relaxed">{question()?.question}</p>

        {/* Hint */}
        <p class="text-xs text-text-weaker">
          <Show when={multi()} fallback={language.t("ui.question.singleHint")}>
            {language.t("ui.question.multiHint")}
          </Show>
        </p>

        {/* Options */}
        <div class="flex flex-col gap-1.5">
          <For each={options()}>
            {(opt, i) => (
              <button
                type="button"
                ref={(el) => (optsRef[i()] = el)}
                disabled={sending()}
                onFocus={() => setStore("focus", i())}
                onClick={() => selectOption(i())}
                class="group flex items-start gap-3 w-full text-left px-3 py-2.5 rounded-lg border transition-all duration-150 focus:outline-none"
                style={{
                  background: picked(opt.label) ? "var(--background-interactive-subtle)" : "var(--background-raised)",
                  "border-color": picked(opt.label) ? "var(--border-interactive-base)" : "var(--border-weak-base)",
                }}
              >
                {/* Check indicator */}
                <span
                  class="mt-0.5 shrink-0 flex items-center justify-center rounded-full border transition-all duration-150"
                  style={{
                    width: "16px",
                    height: "16px",
                    "border-radius": multi() ? "4px" : "50%",
                    background: picked(opt.label) ? "var(--text-interactive-base)" : "transparent",
                    "border-color": picked(opt.label) ? "var(--text-interactive-base)" : "var(--border-base)",
                  }}
                >
                  <Show when={picked(opt.label)}>
                    <svg width="10" height="8" viewBox="0 0 10 8" fill="none" xmlns="http://www.w3.org/2000/svg">
                      <Show
                        when={multi()}
                        fallback={
                          <circle cx="5" cy="4" r="2.5" fill="white" />
                        }
                      >
                        <path d="M1 4L3.5 6.5L9 1.5" stroke="white" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" />
                      </Show>
                    </svg>
                  </Show>
                </span>
                <span class="flex flex-col gap-0.5 min-w-0">
                  <span class="text-sm text-text-strong leading-snug">{opt.label}</span>
                  <Show when={opt.description}>
                    <span class="text-xs text-text-weak leading-snug">{opt.description}</span>
                  </Show>
                </span>
              </button>
            )}
          </For>

          {/* Custom / type own answer option */}
          <Show
            when={store.editing}
            fallback={
              <button
                type="button"
                ref={customRef}
                disabled={sending()}
                onFocus={() => setStore("focus", options().length)}
                onClick={customOpen}
                class="flex items-start gap-3 w-full text-left px-3 py-2.5 rounded-lg border transition-all duration-150 focus:outline-none"
                style={{
                  background: on() ? "var(--background-interactive-subtle)" : "var(--background-raised)",
                  "border-color": on() ? "var(--border-interactive-base)" : "var(--border-weak-base)",
                }}
              >
                <span
                  class="mt-0.5 shrink-0 flex items-center justify-center border transition-all duration-150"
                  style={{
                    width: "16px",
                    height: "16px",
                    "border-radius": multi() ? "4px" : "50%",
                    background: on() ? "var(--text-interactive-base)" : "transparent",
                    "border-color": on() ? "var(--text-interactive-base)" : "var(--border-base)",
                  }}
                >
                  <Show when={on()}>
                    <svg width="10" height="8" viewBox="0 0 10 8" fill="none" xmlns="http://www.w3.org/2000/svg">
                      <Show
                        when={multi()}
                        fallback={<circle cx="5" cy="4" r="2.5" fill="white" />}
                      >
                        <path d="M1 4L3.5 6.5L9 1.5" stroke="white" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" />
                      </Show>
                    </svg>
                  </Show>
                </span>
                <span class="flex flex-col gap-0.5 min-w-0">
                  <span class="text-sm text-text-strong leading-snug">{customLabel()}</span>
                  <span class="text-xs text-text-weak leading-snug">{input() || customPlaceholder()}</span>
                </span>
              </button>
            }
          >
            <form
              class="flex items-start gap-3 w-full px-3 py-2.5 rounded-lg border"
              style={{
                background: "var(--background-interactive-subtle)",
                "border-color": "var(--border-interactive-base)",
              }}
              onMouseDown={(e) => {
                if (sending()) { e.preventDefault(); return }
                if (e.target instanceof HTMLTextAreaElement) return
                const ta = e.currentTarget.querySelector("textarea")
                if (ta) ta.focus()
              }}
              onSubmit={(e) => { e.preventDefault(); commitCustom() }}
            >
              <span
                class="mt-0.5 shrink-0 flex items-center justify-center border"
                style={{
                  width: "16px",
                  height: "16px",
                  "border-radius": multi() ? "4px" : "50%",
                  background: "var(--text-interactive-base)",
                  "border-color": "var(--text-interactive-base)",
                }}
                onClick={toggleCustomMark}
              >
                <svg width="10" height="8" viewBox="0 0 10 8" fill="none" xmlns="http://www.w3.org/2000/svg">
                  <Show
                    when={multi()}
                    fallback={<circle cx="5" cy="4" r="2.5" fill="white" />}
                  >
                    <path d="M1 4L3.5 6.5L9 1.5" stroke="white" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" />
                  </Show>
                </svg>
              </span>
              <span class="flex flex-col gap-1 min-w-0 flex-1">
                <span class="text-sm text-text-strong leading-snug">{customLabel()}</span>
                <textarea
                  ref={focusCustom}
                  placeholder={customPlaceholder()}
                  value={input()}
                  rows={1}
                  disabled={sending()}
                  class="w-full bg-transparent text-sm text-text-strong placeholder:text-text-weaker resize-none focus:outline-none leading-relaxed"
                  onKeyDown={(e) => {
                    if (e.key === "Escape") { e.preventDefault(); setStore("editing", false); focus(options().length); return }
                    if ((e.metaKey || e.ctrlKey) && !e.altKey) return
                    if (e.key !== "Enter" || e.shiftKey) return
                    e.preventDefault()
                    commitCustom()
                  }}
                  onInput={(e) => { customUpdate(e.currentTarget.value); resizeInput(e.currentTarget) }}
                />
              </span>
            </form>
          </Show>
        </div>
      </div>

      {/* Footer */}
      <div class="flex items-center justify-between px-4 py-3 border-t border-border-weak-base bg-background-raised">
        <Button variant="ghost" size="large" disabled={sending()} onClick={reject} aria-keyshortcuts="Escape">
          {language.t("ui.common.dismiss")}
        </Button>
        <div class="flex items-center gap-2">
          <Show when={store.tab > 0}>
            <Button variant="secondary" size="large" disabled={sending()} onClick={back}>
              {language.t("ui.common.back")}
            </Button>
          </Show>
          <Button
            variant={last() ? "primary" : "secondary"}
            size="large"
            disabled={sending()}
            onClick={next}
            aria-keyshortcuts="Meta+Enter Control+Enter"
          >
            {last() ? language.t("ui.common.submit") : language.t("ui.common.next")}
          </Button>
        </div>
      </div>
    </div>
  )
}
