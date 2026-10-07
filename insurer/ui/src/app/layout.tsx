import Link from "next/link";
import "./globals.css";
import { Providers } from "@/components/Providers";

export const metadata = { title: "Insurer Claims Workspace" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <nav>
          <Link href="/">Home</Link> <Link href="/cases">Cases</Link> <Link href="/approvals">Approvals</Link> <Link href="/escalations">Escalations</Link>{" "}
          <Link href="/settlements">Settlements</Link> <Link href="/admin">Admin</Link> <Link href="/login">Sign in</Link>
        </nav>
        <main><Providers>{children}</Providers></main>
      </body>
    </html>
  );
}
