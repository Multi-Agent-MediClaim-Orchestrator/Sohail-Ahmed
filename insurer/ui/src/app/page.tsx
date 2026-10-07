"use client";
import Link from "next/link";
import { useRoles } from "@/components/Providers";

export default function Home() {
  const roles = useRoles();
  return (
    <>
      <h1>Insurer claims workspace</h1>
      <p className="muted">Signed in roles: {roles.length ? roles.join(", ") : "none — use Sign in"}</p>
      <ul>
        <li><Link href="/cases">Cases</Link> — verification workspace</li>
        {(roles.includes("approver") || roles.includes("senior_reviewer")) && <li><Link href="/approvals">Approvals</Link></li>}
        {roles.includes("senior_reviewer") && <li><Link href="/escalations">Escalations</Link></li>}
      </ul>
    </>
  );
}
