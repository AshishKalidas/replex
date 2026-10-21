import { useEffect, useRef, useState } from "react";
import { href } from "../router";
import { Tour } from "./Tour";
import { rememberTour, tourSeen, type TourStep } from "./steps";

const SEEN_KEY = "replex.tour.sample";

/** Marks the overview row the tour opens: the first scenario whose behaviour changed. */
export const CHANGED_SCENARIO = "changed-scenario";

export const SAMPLE_TOUR: [TourStep, ...TourStep[]] = [
  {
    id: "agent",
    page: "overview",
    target: ".page-head h1",
    title: "A support agent, after one small change",
    body: "This sample is a customer-support agent. Someone reworded its instructions to sound friendlier, and RepleX ran four real, recorded conversations through the new version.",
  },
  {
    id: "caught",
    page: "overview",
    target: `[data-tour="${CHANGED_SCENARIO}"]`,
    title: "RepleX caught a problem",
    body: "Two of the four conversations now go differently. Next opens this one.",
  },
  {
    id: "customer",
    page: "scenario",
    target: ".customer blockquote",
    title: "What the customer wrote",
    body: "The same message reaches the agent before and after the change, so any difference comes from the change itself.",
  },
  {
    id: "skipped",
    page: "scenario",
    target: ".action-removed",
    title: "The step it now skips",
    body: "Before the change, the agent checked the refund policy before answering. Now it skips that check and searches the help docs instead, so the policy no longer decides the answer.",
    sides: ["bottom", "top"],
  },
  {
    id: "decide",
    page: "scenario",
    target: ".decision",
    title: "You decide",
    body: "Accept the new behaviour and it becomes the recording your tests expect. Reject it and fix the change. A friendlier tone was the goal; skipping the policy check was not.",
  },
];

/** Open on the first eligible visit only, and say so in storage straight away. */
export function useFirstVisitTour(eligible: boolean): [boolean, (open: boolean) => void] {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (eligible && !tourSeen(SEEN_KEY)) {
      rememberTour(SEEN_KEY);
      setOpen(true);
    }
  }, [eligible]);
  return [open, setOpen];
}

/** The tour of the sample workspace: from the overview into the first changed scenario. */
export function SampleTour({ page, onClose }: { page: string; onClose: () => void }) {
  const scenario = useRef<string | null>(null);
  const navigate = (step: TourStep): boolean => {
    if (step.page === "overview") {
      window.location.hash = href({ view: "overview" });
      return true;
    }
    scenario.current ??= document.querySelector(`[data-tour="${CHANGED_SCENARIO}"] a`)?.getAttribute("href") ?? null;
    if (!scenario.current) return false;
    window.location.hash = scenario.current;
    return true;
  };
  return <Tour steps={SAMPLE_TOUR} page={page} onNavigate={navigate} onClose={onClose} header=".globalnav" />;
}
