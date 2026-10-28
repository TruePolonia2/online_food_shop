"""Contact blueprint and routes for public customer inquiries."""
from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from app.services.contact_service import (
    ContactRateLimitExceededError,
    ContactService,
    ContactValidationError,
)

contact_bp = Blueprint("contact", __name__)


@contact_bp.route("/contact", methods=["GET", "POST"])
def contact():
    """Display public contact form and handle customer support submissions."""
    service = ContactService()

    if request.method == "POST":
        client_ip = request.remote_addr or "127.0.0.1"
        name = request.form.get("name", "")
        email = request.form.get("email", "")
        subject = request.form.get("subject", "")
        message = request.form.get("message", "")

        form_data = {
            "name": name,
            "email": email,
            "subject": subject,
            "message": message,
        }

        try:
            service.submit_message(
                name=name,
                email=email,
                subject=subject,
                message=message,
                client_ip=client_ip,
            )
            flash(
                "Thank you! Your message has been sent successfully. We will review your inquiry shortly.",
                "success",
            )
            return redirect(url_for("contact.contact"))

        except ContactRateLimitExceededError:
            flash(
                "You have submitted too many inquiries recently. Please wait a few minutes before trying again.",
                "error",
            )
            return (
                render_template(
                    "contact.html",
                    errors={"rate_limit": "Too many submissions. Please wait before sending another message."},
                    form_data=form_data,
                ),
                429,
            )

        except ContactValidationError as exc:
            return (
                render_template(
                    "contact.html",
                    errors=exc.errors,
                    form_data=form_data,
                ),
                400,
            )

    return render_template("contact.html", errors={}, form_data={})
