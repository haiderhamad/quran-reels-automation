import { redirect } from "next/navigation";
import { getAdminSession } from "@/lib/auth";

export default async function HomePage() {
  const isAuth = await getAdminSession();
  if (isAuth) {
    redirect("/dashboard");
  } else {
    redirect("/login");
  }
}
