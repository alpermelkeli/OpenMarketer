"use client";

import { usePathname } from "next/navigation";

import { ProjectNav } from "@/components/project/project-nav";
import { projectIdOfPath } from "@/lib/projects/project-navigation";

import { ProjectsNav } from "./projects-nav";

/**
 * The sidebar's navigation. Outside a project it leads to the projects, lists them under that
 * entry and leads to the settings; inside one it becomes that project's own menu.
 */
export function SidebarNav() {
  const pathname = usePathname();
  const projectId = projectIdOfPath(pathname);
  if (projectId !== null) return <ProjectNav projectId={projectId} pathname={pathname} />;
  return <ProjectsNav pathname={pathname} />;
}
