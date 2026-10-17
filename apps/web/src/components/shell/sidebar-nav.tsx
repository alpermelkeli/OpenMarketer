"use client";

import { LayoutGridIcon } from "lucide-react";
import { usePathname } from "next/navigation";

import { ProjectNav } from "@/components/project/project-nav";
import { projectIdOfPath } from "@/lib/projects/project-navigation";

import { NavLink, SettingsLink } from "./nav-link";

/**
 * The sidebar's navigation. Outside a project it leads to the projects and the settings; inside one it
 * becomes that project's own menu, and another project is chosen from the projects page.
 */
export function SidebarNav() {
  const pathname = usePathname();
  const projectId = projectIdOfPath(pathname);
  if (projectId !== null) return <ProjectNav projectId={projectId} pathname={pathname} />;

  return (
    <nav
      aria-label="Main"
      className="flex gap-1 overflow-x-auto px-3 pb-3 lg:flex-1 lg:flex-col lg:overflow-visible lg:pt-2"
    >
      <NavLink href="/" current={pathname === "/"}>
        <LayoutGridIcon aria-hidden="true" />
        Projects
      </NavLink>
      <div className="ml-auto lg:mt-auto lg:ml-0">
        <SettingsLink current={pathname.startsWith("/settings")} />
      </div>
    </nav>
  );
}
