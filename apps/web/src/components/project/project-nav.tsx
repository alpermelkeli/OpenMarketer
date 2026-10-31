"use client";

import {
  ArrowLeftIcon,
  CheckSquareIcon,
  FileTextIcon,
  ImageIcon,
  PenLineIcon,
  PlayCircleIcon,
  PlusIcon,
  TelescopeIcon,
} from "lucide-react";
import Link from "next/link";

import { NavLink, SettingsLink } from "@/components/shell/nav-link";
import { Skeleton } from "@/components/ui/skeleton";
import { useShellChats } from "@/lib/api/chats";
import { useShellProject } from "@/lib/api/projects";
import { useWaitingProposalCount } from "@/lib/api/proposals";
import { shortTitle } from "@/lib/format";
import {
  PROJECT_PAGES,
  chatHref,
  chatIdOfPath,
  projectPageHref,
  projectPageOfPath,
  shortRepositoryName,
} from "@/lib/projects/project-navigation";

import { DeleteProjectButton } from "./delete-project-button";

const PAGE_ICONS = {
  profile: FileTextIcon,
  analyses: PlayCircleIcon,
  research: TelescopeIcon,
  studio: ImageIcon,
  writing: PenLineIcon,
  approvals: CheckSquareIcon,
};

type ProjectNavProps = { projectId: string; pathname: string };

/**
 * The sidebar inside a project: the way back to all projects, which project this is, a new chat, the
 * project's chats and its pages. Another project is chosen from the projects page, not from here.
 */
export function ProjectNav({ projectId, pathname }: ProjectNavProps) {
  const project = useShellProject(projectId);
  const waiting = useWaitingProposalCount(projectId).data;
  const page = projectPageOfPath(pathname);

  return (
    <nav
      aria-label="Project"
      className="flex gap-1 overflow-x-auto px-3 pb-3 lg:flex-1 lg:flex-col lg:overflow-visible lg:pt-1"
    >
      <Link
        href="/"
        className="flex h-8 shrink-0 items-center gap-1.5 rounded-md px-2.5 text-xs text-muted-foreground hover:text-foreground lg:mb-1 lg:h-7"
      >
        <ArrowLeftIcon aria-hidden="true" className="size-3.5" />
        All projects
      </Link>

      <div className="flex shrink-0 items-center gap-1 rounded-md border bg-card pr-1 pl-3 lg:mb-3 lg:items-start lg:py-2">
        <div className="grid min-w-0 flex-1 gap-0.5 py-0.5">
          {project.data ? (
            <>
              <span className="max-w-40 truncate text-sm lg:max-w-none lg:font-heading lg:text-lg lg:leading-tight">
                {project.data.name}
              </span>
              <span className="hidden truncate font-mono text-2xs text-muted-foreground lg:block">
                {shortRepositoryName(project.data.repository_url)}
              </span>
            </>
          ) : project.error ? (
            <span className="text-sm text-muted-foreground">This project could not be read</span>
          ) : (
            <Skeleton className="h-5 w-24 lg:h-10 lg:w-full" />
          )}
        </div>
        {project.data && <DeleteProjectButton project={project.data} />}
      </div>

      <NavLink
        href={projectPageHref(projectId, "chat")}
        current={page === "chat" && chatIdOfPath(pathname) === null}
        className="border bg-card lg:mb-1"
      >
        <PlusIcon aria-hidden="true" />
        New chat
      </NavLink>
      <ChatList projectId={projectId} currentChatId={chatIdOfPath(pathname)} />

      <p className="hidden px-2.5 pt-6 pb-1 text-2xs tracking-widest text-muted-foreground uppercase lg:block">
        Project
      </p>
      {PROJECT_PAGES.map(({ page: target, label }) => {
        const Icon = PAGE_ICONS[target];
        return (
          <NavLink key={target} href={projectPageHref(projectId, target)} current={page === target}>
            <Icon aria-hidden="true" />
            {label}
            {target === "approvals" && waiting !== undefined && waiting > 0 && (
              <span className="ml-auto rounded-full border border-warning/50 px-1.5 font-mono text-2xs text-warning tabular-nums">
                {waiting}
                <span className="sr-only"> waiting for your decision</span>
              </span>
            )}
          </NavLink>
        );
      })}

      <div className="ml-auto lg:mt-auto lg:ml-0">
        <SettingsLink current={false} />
      </div>
    </nav>
  );
}

/** The project's most recently active chats, with a mark on those that have an answer not yet read. */
function ChatList({ projectId, currentChatId }: { projectId: string; currentChatId: string | null }) {
  const chats = useShellChats(projectId);
  return (
    <div className="hidden lg:contents">
      <p className="px-2.5 pt-3 pb-1 text-2xs tracking-widest text-muted-foreground uppercase">Chats</p>
      {chats.data === undefined ? (
        chats.error ? (
          <p className="px-2.5 text-xs text-muted-foreground">The chats could not be read.</p>
        ) : (
          <Skeleton className="mx-2.5 h-5" />
        )
      ) : chats.data.length === 0 ? (
        <p className="px-2.5 text-xs text-muted-foreground">No chats yet.</p>
      ) : (
        <ul aria-label="Chats" className="grid grid-cols-[minmax(0,1fr)] gap-0.5">
          {chats.data.map((chat) => (
            <li key={chat.id}>
              <NavLink href={chatHref(projectId, chat.id)} current={chat.id === currentChatId}>
                <span className="min-w-0 truncate" title={chat.title}>
                  {shortTitle(chat.title)}
                </span>
                {chat.unread && chat.id !== currentChatId && (
                  <span className="ml-auto size-2 shrink-0 rounded-full bg-warning">
                    <span className="sr-only">Unread</span>
                  </span>
                )}
              </NavLink>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
