"use client";

import { FolderIcon, LayoutGridIcon } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils";
import { useRemembered } from "@/lib/remembered/use-remembered";

import { shortId } from "@/lib/format";

/** Navigation: all projects, then the projects this browser remembers. */
export function SidebarNav() {
  const pathname = usePathname();
  const { remembered } = useRemembered();
  const projects = remembered?.projects ?? [];
  const openedIds = remembered?.openedProjectIds ?? [];

  return (
    <nav aria-label="Main" className="flex gap-1 overflow-x-auto px-3 pb-3 lg:flex-col lg:overflow-visible lg:pt-2">
      <NavLink href="/" current={pathname === "/"}>
        <LayoutGridIcon aria-hidden="true" />
        Projects
      </NavLink>
      {projects.length + openedIds.length > 0 && (
        <p className="hidden px-2.5 pt-6 pb-1.5 text-2xs tracking-widest text-muted-foreground uppercase lg:block">
          Remembered here
        </p>
      )}
      {projects.map((project) => (
        <NavLink
          key={project.id}
          href={`/projects/${project.id}`}
          current={pathname.startsWith(`/projects/${project.id}`)}
        >
          <FolderIcon aria-hidden="true" />
          <span className="truncate">{project.name}</span>
        </NavLink>
      ))}
      {openedIds.map((id) => (
        <NavLink key={id} href={`/projects/${id}`} current={pathname.startsWith(`/projects/${id}`)}>
          <FolderIcon aria-hidden="true" />
          <span className="truncate font-mono text-xs">{shortId(id)}</span>
        </NavLink>
      ))}
    </nav>
  );
}

type NavLinkProps = { href: string; current: boolean; children: React.ReactNode };

function NavLink({ href, current, children }: NavLinkProps) {
  return (
    <Link
      href={href}
      aria-current={current ? "page" : undefined}
      className={cn(
        "flex h-8 max-w-56 shrink-0 items-center gap-2.5 rounded-md px-2.5 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground lg:max-w-none [&_svg]:size-4 [&_svg]:shrink-0",
        current && "bg-accent text-foreground",
      )}
    >
      {children}
    </Link>
  );
}
