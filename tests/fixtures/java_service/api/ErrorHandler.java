package io.demo.api;

@RestControllerAdvice
public class ErrorHandler {
  @ExceptionHandler(InvalidRequestException.class)
  public ResponseEntity handleInvalid(InvalidRequestException e) {
    return ResponseEntity.status(422).body(e.getMessage());
  }
}
